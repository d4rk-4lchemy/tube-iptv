import subprocess
from types import SimpleNamespace

import pytest

from app import gpu
from app.engine import ffmpeg_command


@pytest.mark.parametrize('encoder,codec', [('vaapi', 'h264_vaapi'), ('qsv', 'h264_qsv'), ('nvenc', 'h264_nvenc')])
def test_selected_gpu_reaches_ffmpeg(encoder, codec):
    device = '2' if encoder == 'nvenc' else '/dev/dri/renderD129'
    command = ffmpeg_command([], {}, '/unused', encoder=encoder, device=device)
    assert command[command.index('-c:v') + 1] == codec
    if encoder == 'qsv':
        assert f'qsv=hw,child_device={device},child_device_type=vaapi' in command
    else:
        assert command[command.index('-gpu' if encoder == 'nvenc' else '-vaapi_device') + 1] == device


def test_probe_rejects_failed_encoder_and_times_out(monkeypatch):
    monkeypatch.setattr(gpu, 'inventory', lambda: [
        {'id': '1', 'encoders': ['nvenc'], 'accessible': True}])
    def run(command, **kwargs):
        assert command[-3:] == ['-f', 'null', '-']
        assert kwargs['timeout'] == 15
        assert command[command.index('-gpu') + 1] == '1'
        return SimpleNamespace(returncode=1, stderr='Encoder unavailable')
    monkeypatch.setattr(gpu.subprocess, 'run', run)
    with pytest.raises(ValueError, match='Encoder unavailable'):
        gpu.validate('nvenc', '1')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('ffmpeg', 15)
    monkeypatch.setattr(gpu.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='could not complete'):
        gpu.validate('nvenc', '1')


def test_inventory_matches_vendor_and_preserves_inaccessible_devices(tmp_path, monkeypatch):
    dri = tmp_path / 'dri'
    sys = tmp_path / 'sys'
    dri.mkdir()
    for node, vendor in [('renderD128', '0x8086'), ('renderD129', '0x1002')]:
        (dri / node).touch()
        (sys / node / 'device').mkdir(parents=True)
        (sys / node / 'device/vendor').write_text(vendor)
    monkeypatch.setattr(gpu, 'DRI', dri)
    monkeypatch.setattr(gpu, 'SYS_DRM', sys)
    monkeypatch.setattr(gpu.Path, 'is_char_device', lambda self: True)
    monkeypatch.setattr(gpu.os, 'access', lambda path, mode: path.name == 'renderD128')
    monkeypatch.setattr(gpu.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='0, NVIDIA Test GPU\n'))
    devices = gpu.inventory()
    assert devices[0]['encoders'] == ['vaapi', 'qsv']
    assert devices[0]['accessible'] is True
    assert devices[1]['encoders'] == ['vaapi']
    assert devices[1]['accessible'] is False
    assert devices[2]['id'] == '0'
    assert devices[2]['encoders'] == ['nvenc']
