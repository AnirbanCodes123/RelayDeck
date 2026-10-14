# RelayDeck

RelayDeck turns a library of local videos into independent RTSP camera feeds.
It manages up to 110 FFmpeg publishers and routes them through MediaMTX for VLC,
Frigate, FFplay, OpenCV, VMS/NVR software, and custom AI applications.

## Start with Docker

Docker Desktop or Docker Engine with Compose is required.

macOS (Docker Desktop and Python 3.10+, CPU processing):

```bash
./scripts/mac.sh start
```

Ubuntu (CPU processing):

```bash
docker compose up -d --build
```

Compose uses `Dockerfile.ubuntu` by default. The macOS override selects
`Dockerfile.mac`. The builds use separate image tags (`relaydeck-portal:ubuntu`
and `relaydeck-portal:mac`), so one build does not overwrite the other image.
Both run Linux containers and select the FFmpeg package for the target
architecture automatically (ARM64 on Apple Silicon, AMD64 on x86 hosts).

The Mac startup script also starts a native host monitor in the background.
The portal shows the Mac's physical memory (8 GiB on an 8 GB Mac), live CPU
usage, and Apple GPU utilization when macOS exposes it. CPU and Apple GPU
share one unified memory pool; GPU memory is not added to RAM. Memory usage
includes compressed memory's physical footprint and excludes reclaimable
file cache. These are host totals, including other Mac applications.

Metal/MPS remains host-only: RelayDeck's Linux container uses CPU processing.
The edge agent panel continues to monitor processes inside the Linux VM.
If the host monitor stops reporting for 15 seconds, the host panel shows
offline rather than displaying VM memory as Mac memory.

Stop the Mac containers and native monitor with:

```bash
./scripts/mac.sh stop
```

For manual startup, run `python3 scripts/mac_host_monitor.py --background`, then
`docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d --build`.
Monitor logs and telemetry live in the ignored `.mac-monitor/` directory.

NVIDIA GPU server (requires the host NVIDIA driver and NVIDIA Container Toolkit):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Open [http://localhost:9009](http://localhost:9009). Select multiple videos,
review their generated endpoint names, then publish them. Every stream gets a
URL using the address you opened RelayDeck with. For example, opening
`http://192.168.1.50:9009` produces:

```text
rtsp://192.168.1.50:8554/camera-01
```

Test a stream with:

```bash
ffplay -rtsp_transport tcp rtsp://192.168.1.50:8554/camera-01
```

MediaMTX is configured for RTSP-over-TCP only. This prevents RTP packet loss
and visible H.264 slice corruption in VLC when many streams are active. If VLC
is connecting through another RTSP server, enable **RTP over RTSP (TCP)** in
VLC's input/codecs preferences.

## NVIDIA GPU acceleration

The upload queue provides **CPU** and **NVIDIA GPU** processing. Choosing
**NVIDIA GPU** prepares incompatible sources with NVENC. **CPU** prepares
incompatible sources with libx264. H.264/AAC files are stream-copied with either
selection; they do not need an encoder session.

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

RelayDeck reports the detected GPU and whether FFmpeg includes NVENC. If
NVENC fails while preparing a file, the queue retries that file once with CPU
encoding. Use the CPU startup command
for your host above on machines without a GPU.

Uploaded files and the SQLite stream registry live in the `relaydeck-data`
Docker volume. Streams that were running are restored after the application
and MediaMTX restart.

## Scaling model

RelayDeck probes every upload before starting it:

- H.264 video with AAC or no audio uses FFmpeg stream-copy, including sources
  previously saved with NVIDIA processing selected.
- Other sources enter a single conversion queue. Each is converted once to a
  cached H.264/AAC MP4, then published using stream-copy. AVI sources also go
  through preparation to repair unreliable timestamps. The UI shows `preparing`
  until a file is ready; pending streams are not counted as live.
- Choosing NVIDIA uses `h264_nvenc` for preparation, with CPU fallback on
  failure. Only one conversion runs at a time, avoiding many simultaneous
  encoder sessions. Published streams do not occupy NVENC sessions.
- Prepared files persist beside uploads in `.prepared/` inside the data volume.
  They are reused after restart and removed when their stream is deleted.
  Preparation takes time and requires space for both original and prepared
  files. Original uploads are retained.
- RelayDeck converts copied H.264 NAL units to Annex B for reliable RTP
  packetization.

The 110-stream limit is a process-management capacity, not a guarantee that
every server can serve 110 videos at any resolution or bitrate. Stream-copy publishers are lightweight,
but memory, source disk throughput, outgoing network bandwidth, resolution,
bitrate, and consumer count still matter. CPU transcoding should be treated as
a limited fallback.

For a production rollout, load-test in stages:

1. Publish 10 H.264/AAC files and verify stable playback.
2. Increase to 40 while watching CPU, memory, disk reads, and network output.
3. Increase to 80, then 110, only when the host retains sufficient headroom.

A starting host for stream-copy deployments is 8 CPU cores, 16 GB
RAM, SSD/NVMe storage, and network capacity above the sum of source bitrates.
Preparation speed varies with codec and hardware; wait for the conversion
queue to finish before assessing the final number of live streams.

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

- `MAX_STREAMS` (`110`): maximum configured and concurrently active streams.
- `PREPARE_VIDEOS` (`1`): convert incompatible sources once before publishing.
  Set to `0` only for continuous live transcoding; GPU session and memory
  limits then apply to every concurrent transcoded stream.
- `MEDIAMTX_HOST` (`127.0.0.1`): MediaMTX hostname used by FFmpeg.
- `MEDIAMTX_RTSP_PORT` (`8554`): MediaMTX RTSP port.
- `RTSP_PUBLIC_HOST` (unset): use the hostname/IP from the RelayDeck request
  in consumer RTSP URLs. Set this to override the advertised address.
- `DATA_DIR` (`data`): SQLite database and default upload parent.
- `UPLOAD_DIR` (`DATA_DIR/uploads`): uploaded media storage override.
- `DATABASE_PATH` (`DATA_DIR/relaydeck.db`): registry database override.
- `MAX_UPLOAD_BYTES` (`21474836480`): maximum size of each upload, 20 GiB.

Open RelayDeck using the server/laptop LAN IP (for example,
`http://192.168.1.50:9009`) to display and copy RTSP URLs with that IP.
If you open the portal through `localhost` or a reverse proxy, set an explicit
server/laptop IP so the URLs are usable from other computers:

```bash
RTSP_PUBLIC_HOST=192.168.1.50 docker compose up -d
```

On macOS, also pass `-f docker-compose.yml -f docker-compose.mac.yml`.

You can also put `RTSP_PUBLIC_HOST=192.168.1.50` in a `.env` file beside
`docker-compose.yml`. Allow inbound TCP port `8554` for remote RTSP clients.

## Ubuntu build DNS troubleshooting

If the build reports `Temporary failure resolving 'deb.debian.org'`, APT cannot
download its package indexes. Later errors such as `Unable to locate package
curl` are a consequence of that DNS failure.

First check DNS on the Ubuntu host:

```bash
getent hosts deb.debian.org
```

If the host resolves the name but Docker builds cannot, use the optional Linux
build network override:

```bash
sudo docker compose -f docker-compose.yml -f docker-compose.gpu.yml \
  -f docker-compose.build-host.yml up -d --build
```

For CPU-only Ubuntu, omit `-f docker-compose.gpu.yml`. The override uses host
networking only for build steps; service ports and runtime networks retain
their usual configuration. Use this override on Ubuntu/Linux, not macOS.

If the host lookup also fails, fix the Ubuntu host's network/DNS connection
before rebuilding. `RTSP_PUBLIC_HOST` controls advertised stream URLs and
does not configure DNS for Docker builds.

## API summary

- `GET /api/streams`: lightweight stream list, aggregate counts, and services.
- `POST /api/streams`: upload, configure, and optionally start one video.
- `GET /api/streams/{id}`: stream details and recent FFmpeg messages.
- `POST /api/streams/{id}/start`: start one publisher.
- `POST /api/streams/{id}/stop`: stop one publisher.
- `DELETE /api/streams/{id}`: delete one stream and its upload.
- `POST /api/streams/actions/start-all`: start every configured stream.
- `POST /api/streams/actions/stop-all`: stop every publisher.
