"""Local speech model checks; the real engine case is opt-in by model directory."""
import os
from pathlib import Path
import subprocess

import pytest

from genesisai.core.extensions.tools.media.audio_transcribe.implementation import model_directory
from genesisai.shared.security import ToolError
from test_upgrade_p2_p6 import runtime_for, execute


def test_transcription_requires_explicit_complete_local_model(monkeypatch, tmp_path):
    monkeypatch.delenv('GENESISAI_WHISPER_MODEL', raising=False)
    with pytest.raises(ToolError, match='不下载'): model_directory()
    monkeypatch.setenv('GENESISAI_WHISPER_MODEL', str(tmp_path))
    with pytest.raises(ToolError, match='完整本地'): model_directory()


def test_real_synthetic_speech_through_job_and_local_cpu_model(tmp_path, monkeypatch):
    configured = os.environ.get('GENESISAI_TEST_WHISPER_MODEL')
    if not configured or os.name != 'nt': pytest.skip('requires explicit local model directory and Windows SAPI')
    pytest.importorskip('faster_whisper')
    from genesisai.core.jobs import tool_operation
    workspace, _, store, runtime = runtime_for(tmp_path)
    try:
        source = workspace / 'synthetic.wav'
        env = {**os.environ, 'GENESIS_SPEECH_TEST_PATH': str(source)}
        command = "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Speech; $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        command += "$speaker.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet, [System.Speech.Synthesis.VoiceAge]::NotSet, 0, [System.Globalization.CultureInfo]::GetCultureInfo('en-US')); "
        command += "$speaker.SetOutputToWaveFile($env:GENESIS_SPEECH_TEST_PATH); $speaker.Speak('The blue bicycle is beside the quiet river. Please remember the number seven.'); $speaker.Dispose()"
        generated = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
            env=env, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        assert generated.returncode == 0 and source.exists() and source.stat().st_size > 1000, generated.stderr.decode('mbcs', errors='replace')
        monkeypatch.setenv('GENESISAI_WHISPER_MODEL', str(Path(configured).resolve()))
        monkeypatch.setenv('HF_HUB_OFFLINE', '1')
        monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .01)
        submitted = execute(runtime, 'audio_transcribe', {'path': str(source), 'duration_seconds': 30, 'language': 'en'})
        assert submitted['ok'], submitted
        collected = execute(runtime, 'read_job', {'job_id': submitted['data']['job_id'], 'wait_seconds': 30}, 'speech_result')
        assert collected['ok'], collected
        payload = collected['data']['result']
        text = ' '.join(item['text'] for item in payload['items']).casefold()
        assert 'bicycle' in text and 'river' in text, payload
        assert payload['network_used'] is False and payload['engine'] == 'faster_whisper_cpu_int8'
        assert payload['source_ref'] in store.record.sources and payload['next'] is None
        assert payload['coverage']['recognition_is_uncertain']
    finally:
        runtime.close()
