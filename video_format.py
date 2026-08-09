"""The finished video's shape: how big a frame is, and how often one arrives.

Three renderers produce streams that ffmpeg then composites — the scope
waterfall, the HUD bar and the main pass — and a frame rate they disagree on
shows up as drift between the layers rather than as an error. One definition,
imported by all of them.
"""

RESOLUTIONS = {"1080p": (1920, 1080), "720p": (1280, 720)}

RENDER_FPS = 30  # output frame rate; the webcam PiP is resampled to
# this too (see render) so both branches share one
# real-time clock
