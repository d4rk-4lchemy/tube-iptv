"""Output canvases and bounded video rates shared by channel producers."""
# width, height, target/max bitrate (kbit/s). Even 4K stays within the
# existing 8 MiB per-segment limit at the four-second segment cadence.
RESOLUTIONS = {
    '480p': (854, 480, 1500, 2000),
    '720p': (1280, 720, 3000, 4000),
    '1080p': (1920, 1080, 4500, 6000),
    '4k': (3840, 2160, 10000, 12000),
}
