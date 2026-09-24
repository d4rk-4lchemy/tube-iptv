"""GPU inventory and persisted encoding preferences shared by all channels."""
import os
from pathlib import Path
import subprocess

from . import config

DRI = Path('/dev/dri')
SYS_DRM = Path('/sys/class/drm')


def settings(db=None):
    default = {'encoder': config.ENCODER, 'device': config.VAAPI_DEVICE}
    return db.setting('gpu_engine', default) if db else default


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def inventory():
    devices = []
    for path in sorted(DRI.glob('renderD*')):
        if not path.is_char_device():
            continue
        vendor = read(SYS_DRM / path.name / 'device/vendor').lower()
        name = {'0x8086': 'Intel', '0x1002': 'AMD', '0x10de': 'NVIDIA'}.get(vendor, 'GPU')
        drivers = ['vaapi', 'qsv'] if vendor == '0x8086' else ['vaapi']
        devices.append({'id': str(path), 'label': f'{name} · {path}',
                        'encoders': drivers, 'accessible': os.access(path, os.R_OK | os.W_OK)})
    # NVENC uses CUDA ordinals, not DRM render nodes. Also works without /dev/dri.
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=index,name', '--format=csv,noheader,nounits'],
                                capture_output=True, text=True, timeout=5, check=True)
        for line in result.stdout.splitlines():
            index, separator, name = line.partition(',')
            if separator and index.strip().isdigit():
                devices.append({'id': index.strip(), 'label': f'{name.strip()} · NVIDIA {index.strip()}',
                                'encoders': ['nvenc'], 'accessible': True})
    except (OSError, subprocess.SubprocessError):
        pass
    return devices


def validate(encoder, device):
    if encoder == 'software':
        return
    devices = inventory()
    if not any(d['id'] == device and encoder in d['encoders'] and d['accessible'] for d in devices):
        raise ValueError('Select an accessible GPU compatible with this encoder. Refresh the device list if hardware has changed.')
    from .engine import ffmpeg_command
    command = ffmpeg_command([], {}, '', encoder=encoder, device=device, duration=0.2, fps=25, resolution='480p')
    end = next(i for i in range(len(command) - 1) if command[i:i + 2] == ['-f', 'hls'])
    try:
        result = subprocess.run(command[:end] + ['-f', 'null', '-'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('GPU encoding test could not complete. Check FFmpeg and GPU drivers.') from exc
    if result.returncode:
        raise ValueError('GPU encoding test failed. Check device permissions and GPU drivers. ' + result.stderr[-1500:])
