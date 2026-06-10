import re
import yaml
from munch import Munch
import numpy as np
import librosa
import noisereduce as nr
from meldataset import TextCleaner
import torch
import torchaudio
from nltk.tokenize import word_tokenize
import nltk
nltk.download('punkt_tab')

from models import ProsodyPredictor, TextEncoder, StyleEncoder
from Modules.hifigan import Decoder

import sys
import phonemizer
if sys.platform.startswith("win"):
    try:
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        import espeakng_loader
        EspeakWrapper.set_library(espeakng_loader.get_library_path())
    except Exception as e:
        print(e)

def espeak_phn(text, lang):
    try:
        my_phonemizer = phonemizer.backend.EspeakBackend(language=lang, preserve_punctuation=True,  with_stress=True, language_switch='remove-flags')
        return my_phonemizer.phonemize([text])[0]
    except Exception as e:
        print(e)

class Preprocess:
    def __text_normalize(self, text):
        punctuation = ["，", "、", "،", ";", "(", "．", "。", "…", "!", "–", ":", "?"]
        map_to = "."
        punctuation_pattern = re.compile(f"[{''.join(re.escape(p) for p in punctuation)}]")
        #replace punctuation that acts like a comma or period
        text = punctuation_pattern.sub(map_to, text)
        #replace consecutive whitespace chars with a single space and strip leading/trailing spaces
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    def __merge_fragments(self, texts, n):
        merged = []
        i = 0
        while i < len(texts):
            fragment = texts[i]
            j = i + 1
            while len(fragment.split()) < n and j < len(texts):
                fragment += ", " + texts[j]
                j += 1
            merged.append(fragment)
            i = j
        if len(merged[-1].split()) < n and len(merged) > 1: #handle last sentence
            merged[-2] = merged[-2] + ", " + merged[-1]
            del merged[-1]
        else:
            merged[-1] = merged[-1]
        return merged
    def wave_preprocess(self, wave):
        to_mel = torchaudio.transforms.MelSpectrogram(n_mels=80, n_fft=2048, win_length=1200, hop_length=300)
        mean, std = -4, 4
        wave_tensor = torch.from_numpy(wave).float()
        mel_tensor = to_mel(wave_tensor)
        mel_tensor = (torch.log(1e-5 + mel_tensor.unsqueeze(0)) - mean) / std
        return mel_tensor
    def text_preprocess(self, text, n_merge=12):
        text_norm = self.__text_normalize(text).split(".")#split by sentences.
        text_norm = [s.strip() for s in text_norm]
        text_norm = list(filter(lambda x: x != '', text_norm)) #filter empty index
        text_norm = self.__merge_fragments(text_norm, n=n_merge) #merge if a sentence has less that n 
        return text_norm
    def length_to_mask(self, lengths):
        mask = torch.arange(lengths.max()).unsqueeze(0).expand(lengths.shape[0], -1).type_as(lengths)
        mask = torch.gt(mask+1, lengths.unsqueeze(1))
        return mask

#For inference only
class StyleTTS2(torch.nn.Module):
    def __init__(self, config_path, models_path):
        super().__init__()
        self.register_buffer("get_device", torch.empty(0))
        self.preprocess = Preprocess()
        self.ref_s = None
        config = yaml.safe_load(open(config_path, "r", encoding="utf-8"))
        
        try:
            symbols = (
                            list(config['symbol']['pad']) +
                            list(config['symbol']['punctuation']) +
                            list(config['symbol']['letters']) +
                            list(config['symbol']['letters_ipa']) +
                            list(config['symbol']['extend'])
                        )
            symbol_dict = {}
            for i in range(len((symbols))):
                symbol_dict[symbols[i]] = i
            
            n_token = len(symbol_dict) + 1
            print("\nFound:", n_token, "symbols")
        except Exception as e:
            print(f"\nERROR: Cannot find {e} in config file!\nYour config file is likely outdated, please download updated version from the repository.")
            raise SystemExit(1)
        
        args = self.__recursive_munch(config['model_params'])
        args['n_token'] = n_token
        
        self.cleaner = TextCleaner(symbol_dict, debug=False)

        assert args.decoder.type in ['hifigan'], 'Decoder type unknown'

        self.decoder            = Decoder(dim_in=args.hidden_dim, style_dim=args.style_dim, dim_out=args.n_mels,
                                        resblock_kernel_sizes = args.decoder.resblock_kernel_sizes,
                                        upsample_rates = args.decoder.upsample_rates,
                                        upsample_initial_channel=args.decoder.upsample_initial_channel,
                                        resblock_dilation_sizes=args.decoder.resblock_dilation_sizes,
                                        upsample_kernel_sizes=args.decoder.upsample_kernel_sizes)
        self.predictor           = ProsodyPredictor(style_dim=args.style_dim, d_hid=args.hidden_dim, nlayers=args.n_layer, max_dur=args.max_dur, dropout=args.dropout)
        self.text_encoder        = TextEncoder(channels=args.hidden_dim, kernel_size=5, depth=args.n_layer, n_symbols=args.n_token)
        self.style_encoder       = StyleEncoder(dim_in=args.dim_in, style_dim=args.style_dim, max_conv_dim=args.hidden_dim)# acoustic style encoder

        self.__load_models(models_path)
    
    def __recursive_munch(self, d):
        if isinstance(d, dict):
            return Munch((k, self.__recursive_munch(v)) for k, v in d.items())
        elif isinstance(d, list):
            return [self.__recursive_munch(v) for v in d]
        else:
            return d
        
    def __init_replacement_func(self, replacements):
        replacement_iter = iter(replacements)
        def replacement(match):
            return next(replacement_iter)
        return replacement
    
    def __replace_outliers_zscore(self, tensor, threshold=3.0, factor=0.95):
        mean = tensor.mean()
        std = tensor.std()
        z = (tensor - mean) / std

        # Identify outliers
        outlier_mask = torch.abs(z) > threshold
        # Compute replacement value, respecting sign
        sign = torch.sign(tensor - mean)
        replacement = mean + sign * (threshold * std * factor)

        result = tensor.clone()
        result[outlier_mask] = replacement[outlier_mask]

        return result
        
    def __load_models(self, models_path):
        module_params = []
        model = {'decoder':self.decoder, 'predictor':self.predictor, 'text_encoder':self.text_encoder, 'style_encoder':self.style_encoder}

        params_whole = torch.load(models_path, map_location='cpu')
        params = params_whole['net']
        params = {key: value for key, value in params.items() if key in model.keys()}

        for key in model:
            try:
                model[key].load_state_dict(params[key])
            except:
                from collections import OrderedDict
                state_dict = params[key]
                new_state_dict = OrderedDict()
                for k, v in state_dict.items():
                    name = k[7:] # remove `module.`
                    new_state_dict[name] = v
                model[key].load_state_dict(new_state_dict, strict=False)

            total_params = sum(p.numel() for p in model[key].parameters())
            print(key,":",total_params)
            module_params.append(total_params)

        print('\nTotal',":",sum(module_params))

    def __compute_style(self, path, denoise, split_dur):
        device = self.get_device.device
        denoise = min(denoise, 1)
        if split_dur != 0: split_dur = max(int(split_dur), 1)
        max_samples = 24000*20 #max 20 seconds ref audio
        print("Computing the style for:", path)
        
        wave, sr = librosa.load(path, sr=24000)
        audio, index = librosa.effects.trim(wave, top_db=30)
        if sr != 24000:
            audio = librosa.resample(audio, sr, 24000)
        if len(audio) > max_samples:
            audio = audio[:max_samples]
        
        if denoise > 0.0:
            audio_denoise = nr.reduce_noise(y=audio, sr=sr, n_fft=2048, win_length=1200, hop_length=300)
            audio = audio*(1-denoise) + audio_denoise*denoise

        with torch.no_grad():
            if split_dur>0 and len(audio)/sr>=4: #Only effective if audio length is >= 4s
                #This option will split the ref audio to multiple parts, calculate styles and average them
                count = 0
                ref_s = None
                jump = sr*split_dur
                total_len = len(audio)
                
                #Need to init before the loop
                mel_tensor = self.preprocess.wave_preprocess(audio[0:jump]).to(device)
                ref_s = self.style_encoder(mel_tensor.unsqueeze(1))
                count += 1
                for i in range(jump, total_len, jump):
                    if i+jump >= total_len:
                        left_dur = (total_len-i)/sr
                        if left_dur >= 1: #Still count if left over dur is >= 1s
                            mel_tensor = self.preprocess.wave_preprocess(audio[i:total_len]).to(device)
                            ref_s += self.style_encoder(mel_tensor.unsqueeze(1))
                            count += 1
                        continue
                    mel_tensor = self.preprocess.wave_preprocess(audio[i:i+jump]).to(device)
                    ref_s += self.style_encoder(mel_tensor.unsqueeze(1))
                    count += 1
                ref_s /= count
            else:
                mel_tensor = self.preprocess.wave_preprocess(audio).to(device)
                ref_s = self.style_encoder(mel_tensor.unsqueeze(1))

        return ref_s
        
    def __inference(self, phonem, ref_s, speed=1, prev_d_mean=0, t=0.1):
        device = self.get_device.device
        speed = min(max(speed, 0.0001), 2) #speed range [0, 2]
        
        phonem = ' '.join(word_tokenize(phonem))
        tokens = self.cleaner(phonem)
        tokens.insert(0, 0)
        tokens.append(0)
        tokens = torch.LongTensor(tokens).to(device).unsqueeze(0)
        
        with torch.no_grad():
            input_lengths = torch.LongTensor([tokens.shape[-1]]).to(device)
            text_mask = self.preprocess.length_to_mask(input_lengths).to(device)

            # encode
            t_en = self.text_encoder(tokens, input_lengths, text_mask)
            s = ref_s.to(device)
        
            # cal alignment
            d = self.predictor.text_encoder(t_en, s, input_lengths, text_mask)
            x, _ = self.predictor.lstm(d)
            duration = self.predictor.duration_proj(x) 
            duration = torch.sigmoid(duration).sum(axis=-1)

            if prev_d_mean != 0:#Stabilize speaking speed between splits
                dur_stats = torch.empty(duration.shape).normal_(mean=prev_d_mean, std=duration.std()).to(device)
            else:
                dur_stats = torch.empty(duration.shape).normal_(mean=duration.mean(), std=duration.std()).to(device)
            duration = duration*(1-t) + dur_stats*t
            duration[:,1:-2] = self.__replace_outliers_zscore(duration[:,1:-2]) #Normalize outlier

            duration /= speed
                
            pred_dur = torch.round(duration.squeeze()).clamp(min=1)
            pred_aln_trg = torch.zeros(input_lengths, int(pred_dur.sum().data))
            c_frame = 0
            for i in range(pred_aln_trg.size(0)):
                pred_aln_trg[i, c_frame:c_frame + int(pred_dur[i].data)] = 1
                c_frame += int(pred_dur[i].data)
            alignment = pred_aln_trg.unsqueeze(0).to(device)

            # encode prosody
            en = (d.transpose(-1, -2) @ alignment)
            F0_pred, N_pred = self.predictor.F0Ntrain(en, s)
            asr = (t_en @ pred_aln_trg.unsqueeze(0).to(device))

            out = self.decoder(asr, F0_pred, N_pred, s)
        
        return out.squeeze().cpu().numpy(), duration.mean()
    
    def get_styles(self, speakers, denoise=0.3, avg_style=True):
        if avg_style:   split_dur = 2
        else:           split_dur = 0
        styles = {}
        for id in speakers:
            ref_s = self.__compute_style(speakers[id]['path'], denoise=denoise, split_dur=split_dur)
            styles[id] = {
                'style': ref_s,
                'path': speakers[id]['path'],
                'lang': speakers[id]['lang'],
                'speed': speakers[id]['speed'],
            }
        return styles

    def generate(self, text, styles, stabilize=True, n_merge=16, default_speaker= "[id_1]"):
        if stabilize:   smooth_value=0.2
        else:           smooth_value=0    
        
        list_wav        = []
        prev_d_mean     = 0
        lang_pattern    = r'\[([^\]]+)\]\{([^}]+)\}'

        text = re.sub(r'[\n\r\t\f\v]', '', text)
        #fix lang tokens span to multiple sents
        find_lang_tokens = re.findall(lang_pattern, text)
        if find_lang_tokens:
            cus_text = []
            for lang, t in find_lang_tokens:
                parts = self.preprocess.text_preprocess(t, n_merge=0)
                parts = ".".join([f"[{lang}]" + f"{{{p}}}"for p in parts])
                cus_text.append(parts)
            replacement_func = self.__init_replacement_func(cus_text)
            text = re.sub(lang_pattern, replacement_func, text)

        texts = re.split(r'(\[id_\d+\])', text) #split the text by speaker ids while keeping the ids.
        if len(texts) <= 1 or bool(re.match(r'(\[id_\d+\])', texts[0]) == False): #Add a default speaker
            texts.insert(0, default_speaker)
        curr_id = None
        for i in range(len(texts)): #remove consecutive ids
            if bool(re.match(r'(\[id_\d+\])', texts[i])):
                if texts[i]!=curr_id:
                    curr_id = texts[i]
                else:
                    texts[i] = ''
        del curr_id
        texts = list(filter(lambda x: x != '', texts))

        print("Generating Audio...")
        for i in texts:
            if bool(re.match(r'(\[id_\d+\])', i)):
                #Set up env for matched speaker
                speaker_id = i.strip('[]')
                current_ref_s = styles[speaker_id]['style']
                speed = styles[speaker_id]['speed']
                continue
            text_norm = self.preprocess.text_preprocess(i, n_merge=n_merge)
            for sentence in text_norm:
                cus_phonem = []
                find_lang_tokens = re.findall(lang_pattern, sentence)
                if find_lang_tokens:
                    for lang, t in find_lang_tokens:
                        try:
                            phonem = espeak_phn(t, lang)
                            cus_phonem.append(phonem)
                        except Exception as e:
                            print(e)
                        
                replacement_func = self.__init_replacement_func(cus_phonem)
                phonem =  espeak_phn(sentence, styles[speaker_id]['lang'])
                phonem = re.sub(lang_pattern, replacement_func, phonem)

                wav, prev_d_mean = self.__inference(phonem, current_ref_s, speed=speed, prev_d_mean=prev_d_mean, t=smooth_value)
                wav = wav[4000:-4000] #Remove weird pulse and silent tokens
                list_wav.append(wav)
        
        final_wav = np.concatenate(list_wav)
        final_wav = np.concatenate([np.zeros([4000]), final_wav, np.zeros([4000])], axis=0) # add padding
        return final_wav