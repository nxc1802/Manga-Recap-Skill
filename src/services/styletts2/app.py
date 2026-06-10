import gradio as gr
import os
import soundfile as sf
import numpy as np
import torch
import traceback
from inference import StyleTTS2
repo_dir = './'
device = 'cuda' if torch.cuda.is_available() else 'cpu'
config_path = os.path.join(repo_dir, "Models", "config.yaml")
models_path = os.path.join(repo_dir, "Models", "model.pth")
model = StyleTTS2(config_path, models_path).eval().to(device)
voice_path = os.path.join(repo_dir, "reference_audio")
eg_voices = [os.path.join(voice_path,"vn_1.wav"), os.path.join(voice_path,"vn_2.wav")]
eg_texts = [
    "Chỉ với khoảng 90 triệu tham số, [en-us]{StyleTTS2-lite} có thể dễ dàng tạo giọng nói với tốc độ cao.",
    "[id_1] Với [en-us]{StyleTTS2-lite} bạn có thể sử dụng [en-us]{language tag} để mô hình chắc chắn đọc bằng tiếng Anh, [id_2]cũng như sử dụng [en-us]{speaker tag} để chuyển đổi nhanh giữa các giọng đọc.",
]


# Core inference function
def main(reference_paths, text_prompt, denoise, avg_style, stabilize):
    try:
        speakers = {}
        for i, path in enumerate(reference_paths, 1):
            speaker_id = f"id_{i}"
            speakers[speaker_id] = {
                "path": path,
                "lang": "vi",
                "speed": 1.0
        }

        with torch.no_grad():
            styles = model.get_styles(speakers, denoise, avg_style)
            r = model.generate(text_prompt, styles, stabilize, 18, "[id_1]")
            r = r / np.abs(r).max()
            
        sf.write("output.wav", r, samplerate=24000)
        return "output.wav", "Audio generated successfully!"
    
    except Exception as e:
        error_message = traceback.format_exc()
        return None, error_message

def on_file_upload(file_list):
    if not file_list:
        return None, "No file uploaded yet."
    
    unique_files = {}
    for file_path in file_list:
        file_name = os.path.basename(file_path)
        unique_files[file_name] = file_path #update and remove duplicate

    uploaded_infos = []
    uploaded_file_names = list(unique_files.keys())
    for i in range(len(uploaded_file_names)):
        uploaded_infos.append(f"[id_{i+1}]: {uploaded_file_names[i]}")
        
    summary = "\n".join(uploaded_infos)
    return list(unique_files.values()), f"Current reference audios:\n{summary}"

def gen_example(reference_paths, text_prompt):
    output, status = main(reference_paths, text_prompt, 0.6, True, True)
    return output, reference_paths, status


# Gradio UI
with gr.Blocks() as demo:
    gr.HTML("<h1 style='text-align: center;'>StyleTTS2‑Lite Demo</h1>")
    gr.Markdown(
        "Download the local inference package from Hugging Face: "
        "[StyleTTS2‑Lite (Vietnamese)]"
        "(https://huggingface.co/dangtr0408/StyleTTS2-lite-vi/)."
    )
    gr.Markdown(
        "Annotate any non‑Vietnamese words with the appropriate language tag, e.g., [en-us]{  } for English. For more information, see "
        "[eSpeakNG docs]"
        "(https://github.com/espeak-ng/espeak-ng/blob/master/docs/languages.md)"
    )

    with gr.Row(equal_height=True):
        with gr.Column(scale=1):
            text_prompt = gr.Textbox(label="Text Prompt", placeholder="Enter your text here...", lines=4)
        with gr.Column(scale=1):
            avg_style = gr.Checkbox(label="Use Average Styles", value=True)
            stabilize = gr.Checkbox(label="Stabilize Speaking Speed", value=True)
            denoise = gr.Slider(0.0, 1.0, step=0.1, value=0.6, label="Denoise Strength")

    with gr.Row(equal_height=True):
        with gr.Column(scale=1):
            reference_audios = gr.File(label="Reference Audios", file_types=[".wav", ".mp3"], file_count="multiple", height=150)
            gen_button = gr.Button("Generate")
        with gr.Column(scale=1):
            synthesized_audio = gr.Audio(label="Generate Audio", type="filepath")

    status = gr.Textbox(label="Status", interactive=False, lines=3)

    reference_audios.change(
        on_file_upload, 
        inputs=[reference_audios], 
        outputs=[reference_audios, status]
    )

    gen_button.click(
        fn=main,
        inputs=[
            reference_audios,
            text_prompt,
            denoise,
            avg_style,
            stabilize
        ],
        outputs=[synthesized_audio, status]
    )

    gr.Examples(
        examples=[[[eg_voices[0]], eg_texts[0]], [eg_voices, eg_texts[1]]],
        inputs=[reference_audios, text_prompt],
        outputs=[synthesized_audio, reference_audios, status],
        fn=gen_example,
        cache_examples=False,
        label="Examples",
        run_on_click=True
    )

demo.launch()