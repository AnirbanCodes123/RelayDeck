# RelayDeck

RelayDeck turns a library of local videos into independent RTSP camera feeds.
It manages up to 80 FFmpeg publishers and routes them through MediaMTX for VLC,
Frigate, FFplay, OpenCV, VMS/NVR software, and custom AI applications.

## Start with Docker

Docker Desktop or Docker Engine with Compose is required.

CPU-only (macOS, machines without NVIDIA):

```bash
docker compose up --build
```

NVIDIA GPU server (requires the host NVIDIA driver and NVIDIA Container Toolkit):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Open [http://localhost:9009](http://localhost:9009). Select multiple videos,
review their generated endpoint names, then publish them. Every stream gets a
URL such as:

```text
rtsp://localhost:8554/camera-01
```

Test a stream with:

```bash
ffplay -rtsp_transport tcp rtsp://localhost:8554/camera-01
```

MediaMTX is configured for RTSP-over-TCP only. This prevents RTP packet loss
and visible H.264 slice corruption in VLC when many streams are active. If VLC
is connecting through another RTSP server, enable **RTP over RTSP (TCP)** in
VLC's input/codecs preferences.

## NVIDIA GPU acceleration

The upload queue provides **CPU** and **NVIDIA GPU** processing. Choosing
**NVIDIA GPU** encodes every source with NVENC, including H.264/AAC files.
**CPU** still stream-copies compatible H.264/AAC inputs and only transcodes
other codecs with libx264.

On an NVIDIA host, install the NVIDIA driver and NVIDIA Container Toolkit, then
start RelayDeck with both Compose files so the portal container receives the GPU:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

The portal image ships FFmpeg with NVENC. CUDA is not a separate Compose
service: the NVIDIA Container Toolkit injects the host driver (`libcuda`,
NVENC, `nvidia-smi`) into the container. `docker compose up` without
`docker-compose.gpu.yml` never attaches the GPU, so the portal will report
NVIDIA as unavailable.

Confirm the GPU is visible inside the container:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml exec portal nvidia-smi
docker compose -f docker-compose.yml -f docker-compose.gpu.yml exec portal ffmpeg -hide_banner -encoders | grep nvenc
```

RelayDeck performs a real one-frame NVENC test and reports the detected GPU in
the portal. If NVIDIA GPU is selected but NVENC cannot be opened, the stream
automatically falls back to CPU transcoding. The normal `docker compose up`
command remains usable on machines without a GPU.

Uploaded files and the SQLite stream registry live in the `relaydeck-data`
Docker volume. Streams that were running are restored after the application
and MediaMTX restart.

## Scaling model

RelayDeck probes every upload before starting it:

- If **NVIDIA GPU** is selected and NVENC is available, every file is
  transcoded with `h264_nvenc`.
- If **CPU** is selected, H.264 video with AAC or no audio uses FFmpeg
  stream-copy. RelayDeck converts copied H.264 NAL units to Annex B for
  reliable RTP packetization. Other codecs use CPU H.264/AAC transcoding.

The 80-stream limit is a process-management capacity, not a guarantee that
every server can transcode 80 videos. Stream-copy publishers are lightweight,
but memory, source disk throughput, outgoing network bandwidth, resolution,
bitrate, and consumer count still matter. CPU transcoding should be treated as
a limited fallback.

For a production rollout, load-test in stages:

1. Publish 10 H.264/AAC files and verify stable playback.
2. Increase to 40 while watching CPU, memory, disk reads, and network output.
3. Increase to 80 only when the host retains sufficient headroom.

A practical starting host for 80 stream-copy publishers is 8 CPU cores, 16 GB
RAM, SSD/NVMe storage, and network capacity above the sum of source bitrates.
Transcoding requirements vary heavily; reduce `MAX_STREAMS` or pre-convert
media to H.264/AAC when CPU utilization is high.

## Stream lifecycle

- Selecting multiple files creates a browser upload queue with three concurrent
  uploads to avoid saturating the server.
- Each configured stream can be started, stopped, inspected, copied, or deleted
  independently.
- Stopping a stream keeps its upload and configuration.
- Deleting a stream stops FFmpeg and removes its uploaded file.
- **Start all** and **Stop all** control the complete registry.
- A failed FFmpeg process is isolated; other streams continue running.

## Local development

Start MediaMTX separately on port `8554`, then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9009
```

## Configuration

- `MAX_STREAMS` (`80`): maximum configured and concurrently active streams.
- `MEDIAMTX_HOST` (`127.0.0.1`): MediaMTX hostname used by FFmpeg.
- `MEDIAMTX_RTSP_PORT` (`8554`): MediaMTX RTSP port.
- `RTSP_PUBLIC_HOST` (`localhost`): host shown in consumer RTSP URLs.
- `DATA_DIR` (`data`): SQLite database and default upload parent.
- `UPLOAD_DIR` (`DATA_DIR/uploads`): uploaded media storage override.
- `DATABASE_PATH` (`DATA_DIR/relaydeck.db`): registry database override.
- `MAX_UPLOAD_BYTES` (`21474836480`): maximum size of each upload, 20 GiB.

For clients on another computer, set `RTSP_PUBLIC_HOST` to the Docker host's LAN
IP or DNS name and allow inbound TCP port `8554`.

## API summary

- `GET /api/streams`: lightweight stream list, aggregate counts, and services.
- `POST /api/streams`: upload, configure, and optionally start one video.
- `GET /api/streams/{id}`: stream details and recent FFmpeg messages.
- `POST /api/streams/{id}/start`: start one publisher.
- `POST /api/streams/{id}/stop`: stop one publisher.
- `DELETE /api/streams/{id}`: delete one stream and its upload.
- `POST /api/streams/actions/start-all`: start every configured stream.
- `POST /api/streams/actions/stop-all`: stop every publisher.
