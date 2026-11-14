"""Migrated V3 integration scenarios against V4 contracts (L1/L2 only).

All projects, PCM signals and approvals here are isolated test inputs, never
real neural synthesis, human review, Kaggle evidence or release acceptance.
"""
import argparse
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from unit.test_canonical_workflow import unit_project
from src.cli.main import main
from src.cli.exit_codes import CliValidationError
from src.core.storage import read_json, atomic_write_json, calculate_sha256
from src.core.human_review import approve_preview, review_fingerprint
from src.render.chapter_renderer import render_chapter, validate_existing_chapter
from src.render.artifact_validation import fingerprint


def approve_fixture(root, chapter):
    render_chapter(root, '1', quality='preview')
    return approve_preview(root, 'L2 fixture only', review_fingerprint(root), fingerprint(chapter/'video/preview.mp4'))


def test_cli_preview_review_final_export_without_tts(unit_project, monkeypatch, tmp_path, capsys):
    root, chapter = unit_project
    monkeypatch.setenv('MANGA_RECAP_OUTPUT_DIR', str(tmp_path/'export'))
    def forbidden(*a, **kw): raise AssertionError('Render must reuse validated audio')
    monkeypatch.setattr('src.tts.engine.get_tts_engine', forbidden)
    assert main(['render','-p','unit','-c','1','--quality','preview','--json']) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['data']['validation']['valid']
    assert main(['review-pack','-p','unit','-c','1','--format','all','--json']) == 0
    pack = json.loads(capsys.readouterr().out)
    assert pack['status'] == 'success'
    digest = fingerprint(chapter/'video/preview.mp4')
    assert main(['review-approve','-p','unit','--reviewer','L2 fixture only','--config-fingerprint',review_fingerprint(root),'--artifact-hash',digest,'--json']) == 0
    capsys.readouterr()
    assert main(['render','-p','unit','-c','1','--quality','final','--json']) == 0
    result = json.loads(capsys.readouterr().out)['data']
    assert fingerprint(result['video_path']) == digest
    assert fingerprint(tmp_path/'export/unit/chapter_001.mp4') == digest
    assert main(['status','-p','unit','-c','1','--json']) == 0
    assert json.loads(capsys.readouterr().out)['data']['is_complete']


@pytest.mark.parametrize('command', [
    ['render','-c','1','--approved'], ['render','-c','1','--skip-preview'],
    ['compile','--approved'], ['batch','--skip-preview'], ['produce'],
    ['cloud','produce-series','--chapters','1','--skip-preview']])
def test_unsigned_flags_cannot_bypass_g6(unit_project, command, capsys):
    root, chapter = unit_project
    assert main(command+['-p','unit','--json']) == 2
    response = json.loads(capsys.readouterr().out)
    assert response['status'] == 'error'
    assert 'G6' in response['error']['message']
    assert not (chapter/'video/final.mp4').exists()


@pytest.mark.parametrize('damage', ['panel','audio','script','plan','video','voice'])
def test_completed_artifact_mutation_rejected(unit_project, damage):
    root, chapter = unit_project
    approve_fixture(root, chapter)
    render_chapter(root, '1', quality='final')
    paths = dict(panel=chapter/'panels/unit.png',audio=chapter/'audio/clips/unit_signal.wav',
                 script=chapter/'script.json',plan=chapter/'visual_plan.json',
                 video=chapter/'video/final.mp4',voice=root/'voice_selection.json')
    paths[damage].write_bytes(b'corrupted input')
    with pytest.raises(Exception): validate_existing_chapter(root, '1')


@pytest.mark.parametrize('result', ['zero','exception'])
def test_crawl_failure_records_checkpoint_after_valid_setup(unit_project, monkeypatch, capsys, result):
    root, chapter = unit_project
    mocked = AsyncMock(return_value=[])
    if result == 'exception': mocked.side_effect = RuntimeError('HTTP 403 isolated failure')
    monkeypatch.setattr('src.services.crawler_service.crawler_service.crawl_raw_pages', mocked)
    assert main(['crawl','-p','unit','-c','1','--url','https://example.invalid/unit','--json']) == 2
    response = json.loads(capsys.readouterr().out)
    assert '403' in response['error']['message'] if result == 'exception' else 'No raw images' in response['error']['message']
    meta = read_json(chapter/'chapter.json')
    assert meta['stages']['crawl']['status'] == 'failed'
    assert meta['overall_status'] != 'completed'


def test_force_segment_invalidates_downstream(unit_project, capsys):
    root, chapter = unit_project
    shutil.copy2(chapter/'panels/unit.png', chapter/'raw/page.png')
    for path in [chapter/'video/final.mp4',chapter/'subtitles/final.srt']:
        path.write_bytes(b'stale fixture')
    assert main(['segment','-p','unit','-c','1','--force','--json']) == 0
    assert json.loads(capsys.readouterr().out)['data']['total_panels'] > 0
    assert not (chapter/'audio/clips/unit_signal.wav').exists()
    assert not (chapter/'video/final.mp4').exists()
    assert not (chapter/'subtitles/final.srt').exists()
    meta = read_json(chapter/'chapter.json')
    assert meta['stages']['render']['status'] == 'pending'
    assert meta['human_review_status'] == 'unreviewed'


@pytest.mark.parametrize('stage', ['segment','render'])
def test_execution_failure_cannot_complete(unit_project, monkeypatch, stage):
    root, chapter = unit_project
    def crash(*a, **kw): raise RuntimeError('injected deterministic failure')
    if stage == 'segment':
        from src.cli.segment_cmd import handle_segment
        shutil.copy2(chapter/'panels/unit.png', chapter/'raw/page.png')
        monkeypatch.setattr('src.core.streaming_segmenter.StreamingSegmenter.extract_panels', crash)
        call = lambda: handle_segment(argparse.Namespace(project='unit',chapter='1',force=True))
    else:
        monkeypatch.setattr('src.render.chapter_renderer.render_visual_plan', crash)
        call = lambda: render_chapter(root,'1',quality='preview')
    with pytest.raises(RuntimeError, match='injected'): call()
    meta = read_json(chapter/'chapter.json')
    assert meta['stages'][stage]['status'] == 'failed'
    assert meta['overall_status'] != 'completed'


@pytest.mark.parametrize('mutation', ['unchanged','text','emotion','force','corrupt'])
def test_tts_cache_and_invalidation_with_injected_unit_engine(unit_project, monkeypatch, mutation):
    """Tests orchestration only; the injected signal is explicitly not neural evidence."""
    from src.cli.tts_cmd import handle_tts
    root, chapter = unit_project
    signal = (chapter/'audio/clips/unit_signal.wav').read_bytes()
    calls=[]
    class UnitEngine:
        mode='neural'  # interface under test, not a neural production claim
        init_duration_seconds=total_inference_seconds=0.0
        utterance_count=0
        def synthesize_utterance(self, **kw):
            calls.append(kw['text']); Path(kw['output_path']).write_bytes(signal)
            return .5, 'ignored-untrusted-checksum', False
    monkeypatch.setattr('src.cli.tts_cmd.get_tts_engine', lambda **kw: UnitEngine())
    args=argparse.Namespace(project='unit',chapter='1',workers=1)
    first=handle_tts(args)
    assert first['segments_synthesized']==1
    data=read_json(chapter/'script.json'); seg=data['segments'][0]
    assert seg['checksum']==calculate_sha256(chapter/seg['audio_path'])
    if mutation=='text': seg['narrator_text']='Changed fixture narration'
    elif mutation=='emotion': seg['emotion']='anger'
    elif mutation=='force': args.force=True
    elif mutation=='corrupt': (chapter/seg['audio_path']).write_bytes(b'RIFFbroken')
    atomic_write_json(chapter/'script.json',data)
    stale=chapter/'video/final.mp4'; stale.write_bytes(b'stale')
    second=handle_tts(args)
    if mutation=='unchanged':
        assert second['segments_cached']==1 and len(calls)==1 and stale.exists()
    else:
        assert second['segments_synthesized']==1 and len(calls)==2 and not stale.exists()


@pytest.mark.parametrize('engine', ['mock','unavailable'])
def test_tts_unavailable_or_mock_never_completes(unit_project, monkeypatch, engine):
    from src.cli.tts_cmd import handle_tts
    root, chapter = unit_project
    if engine == 'unavailable':
        def missing(**kw): raise RuntimeError('Neural dependency absent')
        monkeypatch.setattr('src.cli.tts_cmd.get_tts_engine', missing)
    with pytest.raises((CliValidationError,RuntimeError)):
        handle_tts(argparse.Namespace(project='unit',chapter='1',engine='mock' if engine=='mock' else 'neural'))
    assert read_json(chapter/'chapter.json')['stages']['tts']['status']=='failed'


def test_compile_validated_final_and_feature_timeline(unit_project, tmp_path, monkeypatch):
    from src.cli.compile_cmd import handle_compile
    root, chapter = unit_project
    monkeypatch.setenv('MANGA_RECAP_OUTPUT_DIR',str(tmp_path/'exports'))
    approve_fixture(root, chapter)
    render_chapter(root,'1',quality='final')
    from src.core.storage import init_chapter_layout
    other=init_chapter_layout('unit','2')
    shutil.copytree(chapter/'panels',other/'panels',dirs_exist_ok=True)
    shutil.copytree(chapter/'audio',other/'audio',dirs_exist_ok=True)
    for name in ['script.json','manifest.json']:
        data=read_json(chapter/name); data['chapter_number']='2'; atomic_write_json(other/name,data)
    render_chapter(root,'2',quality='final')
    result=handle_compile(argparse.Namespace(project='unit',chapters='1,2',output=str(tmp_path/'compiled.mp4')))
    assert result['chapters_compiled']==['1','2']
    timeline=read_json(result['timeline_path'])
    assert len(timeline['chapters'])==2
    assert Path(result['compiled_video']).is_file()
    assert Path(result['timeline_path']).is_file()


@pytest.mark.parametrize('bypass', [False,True])
def test_cleanup_requires_receipts_and_retains_real_media(unit_project, bypass):
    from src.cli.clean_cmd import handle_clean
    root, chapter=unit_project
    approve_fixture(root,chapter)
    render_chapter(root,'1',quality='final')
    video=chapter/'video/final.mp4'; digest=fingerprint(video)
    with pytest.raises(CliValidationError):
        handle_clean(argparse.Namespace(project='unit',chapter='1',dry_run=False,confirm_project='unit',bypass_receipts=bypass))
    assert fingerprint(video)==digest


@pytest.mark.parametrize('corrupt', [False,True])
def test_batch_uses_artifact_cache_and_isolates_failure(unit_project, monkeypatch, tmp_path, corrupt):
    from src.cli.batch_cmd import handle_batch
    from src.core.storage import init_chapter_layout
    root, chapter=unit_project
    monkeypatch.setenv('MANGA_RECAP_OUTPUT_DIR',str(tmp_path/'exports'))
    approve_fixture(root,chapter)
    render_chapter(root,'1',quality='final')
    other=init_chapter_layout('unit','2')
    shutil.copytree(chapter/'panels',other/'panels',dirs_exist_ok=True)
    shutil.copytree(chapter/'audio',other/'audio',dirs_exist_ok=True)
    for name in ['script.json','manifest.json']:
        data=read_json(chapter/name); data['chapter_number']='2'; atomic_write_json(other/name,data)
    render_chapter(root,'2',quality='final')
    if corrupt: (other/'video/final.mp4').write_bytes(b'not a real video')
    def no_synthesis(*a,**kw): raise RuntimeError('isolated synthesis unavailable')
    monkeypatch.setattr('src.cli.tts_cmd.handle_tts',no_synthesis)
    result=handle_batch(argparse.Namespace(project='unit',chapters='2,1'))
    assert '1' in result['cached']
    if corrupt:
        assert not result['success'] and result['failed'][0]['chapter']=='2'
    else:
        assert result['success'] and result['cached']==['2','1']
    assert (tmp_path/'exports/unit/chapter_001.mp4').is_file()


def test_runner_resume_revalidates_completed_media(unit_project, tmp_path):
    from src.production.runner import SingleSessionProductionRunner
    from src.production.schemas import ProductionJobConfig, ProductionStageStatus
    root,chapter=unit_project
    approve_fixture(root,chapter); render_chapter(root,'1',quality='final')
    config=ProductionJobConfig(project_slug='unit',target_chapters=['1'],tts_engine='neural')
    runner=SingleSessionProductionRunner.load_or_create('unit',config=config)
    runner.manifest.status=ProductionStageStatus.COMPLETED
    assert runner.run().status==ProductionStageStatus.COMPLETED
    (chapter/'video/final.mp4').write_bytes(b'corrupt')
    with pytest.raises(Exception): runner.run()


@pytest.mark.parametrize('kind,code', [('dependency',4),('execution',1),('io',3)])
def test_cli_operational_error_codes_after_gates(unit_project, monkeypatch, capsys, kind, code):
    from src.core.utils import DependencyNotFoundError
    root,chapter=unit_project
    approve_fixture(root,chapter)
    errors=dict(dependency=DependencyNotFoundError('Missing deterministic binary'),
                execution=RuntimeError('ffmpeg operation failed'),io=PermissionError('isolated IO failure'))
    def fail(*a,**kw): raise errors[kind]
    monkeypatch.setattr('src.cli.compile_cmd.handle_compile',fail)
    assert main(['compile','-p','unit','--json'])==code
    envelope=json.loads(capsys.readouterr().out)
    assert envelope['status']=='error' and envelope['exit_code']==code


@pytest.mark.parametrize('job', ['tts','render','produce'])
def test_cloud_packaging_and_job_separation(unit_project,tmp_path,job):
    import tarfile
    from src.cloud.kaggle_service import KaggleCloudService
    from src.cloud.payload import verify_payload_manifest
    root,chapter=unit_project
    service=object.__new__(KaggleCloudService)
    archive=service.package_chapter_payload('unit','1',tmp_path/'payload.tar.gz',job_type=job)
    unpack=tmp_path/'unpack';unpack.mkdir()
    with tarfile.open(archive) as tar: tar.extractall(unpack,filter='data')
    manifest=verify_payload_manifest(unpack,service._payload_manifest_hash)
    assert manifest['job_type']==job
    assert service._dispatch_input_hashes['chapters/1/script.json']==fingerprint(chapter/'script.json')
    code=service.generate_worker_script('unit','1',job_type=job,encoder_preference='libx264')
    compile(code,'worker.py','exec')
    assert 'from src.cloud.canonical_worker import run' in code
    # Runtime inventory, not obsolete source strings, defines the cloud contract.
    for path in ['src/render/visual_policy.py','src/render/visual_plan_renderer.py','src/cli/tts_cmd.py']:
        assert manifest['files'][path]==fingerprint(Path(path))


@pytest.mark.parametrize('state', ['FAILED','TIMEOUT','NOT_COMPLETE','DISPATCH'])
def test_cloud_failure_timeout_never_syncs(unit_project,monkeypatch,state):
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from src.cloud.kaggle_service import KaggleCloudService
    root,chapter=unit_project
    service=object.__new__(KaggleCloudService)
    service._api=MagicMock(); service._username='unit-fixture';service.api_token='secret-unit-token'
    service._api.dataset_status.return_value=SimpleNamespace(status='ready')
    service._api.dataset_list_files.return_value=SimpleNamespace(dataset_files=[SimpleNamespace(name='payload.tar.gz')])
    service._api.kernels_push.return_value=SimpleNamespace(ref='unit-fixture/run')
    service._api.kernels_status.return_value=SimpleNamespace(status=state,failureMessage='injected CUDA failure')
    service.sync_results=MagicMock(side_effect=AssertionError('failed job cannot sync'))
    monkeypatch.setattr('src.cloud.kaggle_service.time.sleep',lambda *_:None)
    before=(chapter/'chapter.json').read_bytes()
    if state=='DISPATCH':
        result=service.dispatch_chapter_job('unit','1',wait=False,job_type='render')
        assert result['status']=='dispatched'
        staging=Path(result['staging_dir'])
        for path in staging.rglob('*'):
            if path.is_file() and path.suffix in {'.json','.py'}:
                assert service.api_token not in path.read_text()
        metadata=read_json(staging/'kernel-metadata.json')
        assert metadata['is_private'] is True and metadata['enable_gpu'] is True
        ds=read_json(staging/'dataset/dataset-metadata.json')
        assert ds['licenses']==[{'name':'other'}]
    else:
        with pytest.raises((RuntimeError,TimeoutError)):
            service.dispatch_chapter_job('unit','1',job_type='render',timeout_seconds=0 if state=='TIMEOUT' else .01,poll_interval=0)
    service.sync_results.assert_not_called()
    assert (chapter/'chapter.json').read_bytes()==before


def test_future_dual_t4_entrypoints_remain_disabled(unit_project, tmp_path):
    """P8 Dual-T4 series entrypoints enforce strict fail-closed Q01-Q10, G6 & G8 gates."""
    from src.cloud.kaggle_service import KaggleCloudService
    from src.cloud.series_payload import G8_REQUIRED_RECEIPTS
    root, chapter = unit_project
    service = object.__new__(KaggleCloudService)
    service._api = None
    service._username = 'unit-fixture'

    # 1. package_series_payload & dispatch_series_job fail closed without Q01-Q10 or G6 approval
    with pytest.raises(CliValidationError, match='Strict Form Gate'):
        service.package_series_payload('missing_series', ['1'], tmp_path / 'bad.tar.gz')
    with pytest.raises(CliValidationError, match='Gate G6'):
        service.package_series_payload('unit', ['1'], tmp_path / 'unapproved.tar.gz', skip_preview=True)
    with pytest.raises(ValueError, match='Gate G6'):
        service.dispatch_series_job('unit', ['1'], skip_preview=True)

    # Approve Chapter 1 preview -> package_series_payload + generate_series_worker_script succeed
    approve_fixture(root, chapter)
    archive = service.package_series_payload('unit', ['1'], tmp_path / 'series.tar.gz')
    assert archive.is_file()
    worker_script = service.generate_series_worker_script('unit', ['1'], encoder_preference='libx264')
    compile(worker_script, 'series_worker.py', 'exec')
    assert service._series_payload_manifest_hash in worker_script
    assert worker_script.index('Payload inventory mismatch') < worker_script.index("pip', 'install'")

    # 2. sync_series_results rejects missing G8 GPU0/GPU1 telemetry or failed semantic parity
    bad_results = tmp_path / 'bad_results'
    bad_results.mkdir()
    with pytest.raises(ValueError, match='Missing trusted series dispatch input snapshot|Missing G8'):
        service.sync_series_results('unit', ['1'], bad_results)

    # Even with a valid result manifest, missing GPU1 real work in G8 receipts must fail closed
    for fname in ('dual_t4_environment.json', 'dual_t4_gpu_trace.csv', 'dual_t4_worker_log.jsonl', 'dual_t4_artifact_report.json'):
        if fname == 'dual_t4_environment.json':
            atomic_write_json(bad_results / fname, {'gpu_count': 2, 'mock_mode': False, 'gpus': [{'gpu_id': 0, 'gpu_uuid': 'u0'}, {'gpu_id': 1, 'gpu_uuid': 'u1'}]})
        elif fname == 'dual_t4_gpu_trace.csv':
            (bad_results / fname).write_text('gpu_id,vram_used_mb\n0,500\n', encoding='utf-8')
        elif fname == 'dual_t4_worker_log.jsonl':
            (bad_results / fname).write_text(json.dumps({'event': 'tts_batch_completed', 'gpu_id': 0}) + '\n', encoding='utf-8')
        else:
            atomic_write_json(bad_results / fname, {'gpu_usage_evidence': {'0': {'tts_items': 1}, '1': {'tts_items': 0, 'render_shots': 0}}, 'per_chapter': {'1': {}}})
    files_inv = {p.relative_to(bad_results).as_posix(): fingerprint(p) for p in bad_results.rglob('*') if p.is_file()}
    atomic_write_json(bad_results / 'dual_t4_result_manifest.json', {
        'schema_version': '4.2.0', 'project_slug': 'unit', 'chapters': ['1'], 'job_type': 'produce_series',
        'payload_manifest_sha256': service._series_payload_manifest_hash, 'files': files_inv,
    })
    with pytest.raises(ValueError, match='G8'):
        service.sync_series_results('unit', ['1'], bad_results)
    assert len(G8_REQUIRED_RECEIPTS) == 5


@pytest.mark.parametrize('mutation', ['voice_override','speed_override','segment_voice'])
def test_single_narrator_setup_overrides_rejected(unit_project,mutation):
    from src.cli.tts_cmd import handle_tts
    root,chapter=unit_project
    args=argparse.Namespace(project='unit',chapter='1')
    if mutation=='voice_override': args.voice='Trúc Ly'
    elif mutation=='speed_override': args.speed=1.25
    else:
        data=read_json(chapter/'script.json');data['segments'][0]['voice_id']='Trúc Ly'
        atomic_write_json(chapter/'script.json',data)
        # Reject voice contradictions before model loading as well.
    if mutation=='segment_voice':
        from unittest.mock import patch,MagicMock
        with patch('src.cli.tts_cmd.get_tts_engine',return_value=MagicMock(mode='neural')):
            with pytest.raises(CliValidationError,match='conflicts'): handle_tts(args)
    else:
        with pytest.raises(CliValidationError,match='conflicts'): handle_tts(args)


def test_opaque_segment_sequence_and_custom_evidence_path(unit_project):
    root,chapter=unit_project
    renamed=chapter/'panels/authored_evidence.png'
    (chapter/'panels/unit.png').rename(renamed)
    manifest=read_json(chapter/'manifest.json')
    manifest['panels'][0].update(filename=renamed.name,relative_path='panels/'+renamed.name)
    atomic_write_json(chapter/'manifest.json',manifest)
    script=read_json(chapter/'script.json');seg=script['segments'][0]
    script['segments']=[dict(seg,seq_id='opening'),dict(seg,seq_id='002_1',narrator_text='Second unit segment')]
    atomic_write_json(chapter/'script.json',script)
    result=render_chapter(root,'1',quality='preview')
    assert result['validation']['valid']
    plan=read_json(chapter/'visual_plan.json')
    assert [s['segment_id'] for s in plan['shots']]==['opening','002_1']
    assert plan['total_duration_sec']==1.0
    assert sum(s['duration_sec'] for s in plan['shots'])==1.0
    assert (chapter/'subtitles/preview.srt').read_text().count('-->')==2
