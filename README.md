# Private OpenList NVENC transcode worker

This private image builds the standalone `cmd/worker` program from the official
OpenList repository and runs it with a Debian FFmpeg package. The worker image
is intended to be run with NVIDIA Container Toolkit and a host GPU that supports
NVENC.

The GitHub Actions workflow fetches the requested upstream OpenList tag or commit
at build time. The private repository does not vendor the OpenList source.

## Required master settings

- `media_enabled=true`
- `transcode_enabled=true`
- `transcode_run_mode=remote`
- `transcode_worker_secret=<long-random-secret>`
- `transcode_hwaccel=nvenc`
- `transcode_output_codec=hevc` or `h264`

Use `remote` first so the Alpine master worker cannot claim the job before the
NVENC worker. Switch to `hybrid` only after the remote worker is verified.

## Runtime check

Inside the worker container, verify:

```sh
ffmpeg -hide_banner -encoders | grep -E 'h264_nvenc|hevc_nvenc'
ffmpeg -hide_banner -hwaccels | grep cuda
```

The GitHub-hosted build runner has no NVIDIA GPU, so the actual NVENC test must
run on the target server after the image is pulled.
