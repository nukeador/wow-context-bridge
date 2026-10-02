/*
 * Standalone native PipeWire ScreenCast probe for the WoW Context Bridge experimental reader.
 * It consumes an OpenPipeWireRemote() FD, connects to the portal's numeric node
 * ID (ScreenCast v5), requests CPU-readable video and saves the first complete
 * MemPtr or readable/mappable MemFd frame. It does not inspect or control the game process.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <limits.h>
#include <sys/mman.h>

#include <pipewire/pipewire.h>
#include <spa/buffer/buffer.h>

/* SPA 1.6 adds the public name; keep the numeric protocol flag available when
 * building with older PipeWire headers (the Deck runtime advertises it). */
#ifndef SPA_DATA_FLAG_MAPPABLE
#define SPA_DATA_FLAG_MAPPABLE (1u << 3)
#endif
#include <spa/buffer/meta.h>
#include <spa/param/buffers.h>
#include <spa/param/format.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/video/raw.h>
#include <spa/pod/builder.h>

#define MAX_PLANES 8
#define MAX_FRAME_BYTES (512u * 1024u * 1024u)
#define STREAM_CROP_MAX_WIDTH 1088u
#define STREAM_CROP_MAX_HEIGHT 128u
#define STREAM_MAX_FPS 2u

static volatile sig_atomic_t stop_requested = 0;

struct saved_plane {
  uint32_t data_index;
  uint32_t chunk_offset;
  uint32_t size;
  int32_t stride;
  uint8_t *bytes;
};

struct probe {
  struct pw_main_loop *main_loop;
  struct pw_stream *stream;
  struct spa_hook core_listener;
  struct spa_hook stream_listener;
  uint32_t node_id;
  int timeout_ms;
  int stream_output_fd;
  int stream_interval_ms;
  uint64_t last_stream_attempt_ns;
  uint64_t stream_frames_sent;
  uint32_t requested_width;
  uint32_t requested_height;
  const char *output_prefix;
  struct spa_video_info_raw video;
  int have_format;
  int stream_error;
  int connect_error;
  int frame_saved;
  int core_sync_seq;
  int core_sync_done;
  uint64_t process_callbacks;
  uint64_t buffers_seen;
  uint64_t first_cpu_buffer;
  uint32_t plane_count;
  struct saved_plane planes[MAX_PLANES];
};

static void on_signal(int signo) {
  (void)signo;
  stop_requested = 1;
}

static double monotonic_seconds(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0.0;
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static const char *param_name(uint32_t id) {
  switch (id) {
    case SPA_PARAM_Format: return "Format";
    case SPA_PARAM_Buffers: return "Buffers";
    case SPA_PARAM_Meta: return "Meta";
    case SPA_PARAM_IO: return "IO";
    default: return "other";
  }
}

static void on_core_info(void *data, const struct pw_core_info *info) {
  (void)data;
  printf("event=core.info version=%s\n", info && info->version ? info->version : "unknown");
  fflush(stdout);
}

static void on_core_error(void *data, uint32_t id, int seq, int res,
                          const char *message) {
  struct probe *p = data;
  p->stream_error = 1;
  printf("event=core.error id=%u seq=%d result=%d message=%s\n",
         id, seq, res, message ? message : "");
  fflush(stdout);
}

static void on_core_done(void *data, uint32_t id, int seq) {
  struct probe *p = data;
  printf("event=core.done id=%u seq=%d\n", id, seq);
  fflush(stdout);
  if (id == PW_ID_CORE && seq == p->core_sync_seq)
    p->core_sync_done = 1;
}

static const struct pw_core_events core_events = {
  .version = PW_VERSION_CORE_EVENTS,
  .info = on_core_info,
  .done = on_core_done,
  .error = on_core_error,
};

static void on_state_changed(void *data, enum pw_stream_state old_state,
                             enum pw_stream_state state, const char *error) {
  struct probe *p = data;
  printf("event=stream.state old=%s new=%s error=%s\n",
         pw_stream_state_as_string(old_state),
         pw_stream_state_as_string(state),
         error ? error : "");
  fflush(stdout);
  if (state == PW_STREAM_STATE_ERROR) {
    p->stream_error = 1;
  }
}

static void on_param_changed(void *data, uint32_t id,
                             const struct spa_pod *param) {
  struct probe *p = data;
  printf("event=stream.param id=%u name=%s present=%s\n",
         id, param_name(id), param ? "true" : "false");
  fflush(stdout);
  if (id != SPA_PARAM_Format || param == NULL) return;

  uint32_t media_type = 0;
  uint32_t media_subtype = 0;
  if (spa_format_parse(param, &media_type, &media_subtype) < 0) {
    printf("event=stream.format_parse_error stage=media\n");
    fflush(stdout);
    return;
  }
  if (media_type != SPA_MEDIA_TYPE_video ||
      media_subtype != SPA_MEDIA_SUBTYPE_raw) {
    printf("event=stream.format_unhandled media_type=%u media_subtype=%u\n",
           media_type, media_subtype);
    fflush(stdout);
    return;
  }

  struct spa_video_info_raw raw = {0};
  if (spa_format_video_raw_parse(param, &raw) < 0) {
    printf("event=stream.format_parse_error stage=video_raw\n");
    fflush(stdout);
    return;
  }

  p->video = raw;
  p->have_format = 1;
  printf("event=stream.format media=video subtype=raw format=%u "
         "width=%u height=%u framerate=%u/%u max_framerate=%u/%u\n",
         raw.format, raw.size.width, raw.size.height,
         raw.framerate.num, raw.framerate.denom,
         raw.max_framerate.num, raw.max_framerate.denom);
  fflush(stdout);

  uint8_t storage[1024];
  struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
  const struct spa_pod *params[2];
  /* The Deck actually delivered readable, mappable MemFd buffers. Request
   * only that known-supported type instead of claiming MemPtr support alone. */
  const int requested_buffer_types = (int)(1u << SPA_DATA_MemFd);
  params[0] = spa_pod_builder_add_object(
      &builder, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
      SPA_PARAM_BUFFERS_dataType,
      SPA_POD_Int(requested_buffer_types),
      0);
  params[1] = spa_pod_builder_add_object(
      &builder, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
      SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
      SPA_PARAM_META_size, SPA_POD_Int((int)sizeof(struct spa_meta_header)),
      0);
  int result = pw_stream_update_params(p->stream, params, 2);
  printf("event=stream.request_memfd_buffers mask=0x%x result=%d\n",
         requested_buffer_types, result);
  fflush(stdout);
}

struct mapped_plane {
  const uint8_t *base;
  void *mapping;
  size_t mapping_size;
};

static void release_mapped_planes(struct mapped_plane *planes, uint32_t count) {
  for (uint32_t i = 0; i < count; i++) {
    if (planes[i].mapping && planes[i].mapping != MAP_FAILED)
      munmap(planes[i].mapping, planes[i].mapping_size);
    planes[i] = (struct mapped_plane){0};
  }
}

static int map_readable_plane(const struct spa_data *data,
                              struct mapped_plane *out) {
  if (!data || !(data->flags & SPA_DATA_FLAG_READABLE) || data->maxsize == 0)
    return 0;
  if (data->data) {
    out->base = data->data;
    return 1;
  }
  if (data->type != SPA_DATA_MemFd ||
      !(data->flags & SPA_DATA_FLAG_MAPPABLE) || data->fd < 0 ||
      data->fd > INT_MAX)
    return 0;

  long page_size = sysconf(_SC_PAGESIZE);
  if (page_size <= 0) return 0;
  uint64_t map_offset = data->mapoffset;
  uint64_t aligned_offset = map_offset - (map_offset % (uint64_t)page_size);
  size_t delta = (size_t)(map_offset - aligned_offset);
  if ((size_t)data->maxsize > SIZE_MAX - delta) return 0;
  size_t map_size = delta + (size_t)data->maxsize;
  void *mapping = mmap(NULL, map_size, PROT_READ, MAP_SHARED,
                       (int)data->fd, (off_t)aligned_offset);
  if (mapping == MAP_FAILED) {
    printf("event=stream.buffer_map_error fd=%" PRId64
           " mapoffset=%u maxsize=%u errno=%d message=%s\n",
           data->fd, data->mapoffset, data->maxsize, errno, strerror(errno));
    return 0;
  }
  out->mapping = mapping;
  out->mapping_size = map_size;
  out->base = (const uint8_t *)mapping + delta;
  return 1;
}

static uint64_t monotonic_nanoseconds(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
  return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}

static int write_all(int fd, const uint8_t *data, size_t size) {
  while (size > 0) {
    ssize_t written = write(fd, data, size);
    if (written < 0 && errno == EINTR) continue;
    if (written <= 0) return -1;
    data += written;
    size -= (size_t)written;
  }
  return 0;
}

/* Emit a small RGB crop over a dedicated inherited pipe FD. The crop contains
 * the decoder's top-left search area and enough room for the largest v1 strip;
 * it is never written to disk. Header: DCPF, width:u16, height:u16, monotonic_ns:u64.
 */
static int stream_frame_crop(struct probe *p, struct pw_buffer *pwbuf,
                             uint64_t frame_index, uint64_t timestamp_ns) {
  if (!pwbuf || !pwbuf->buffer || !p->have_format ||
      p->video.format != SPA_VIDEO_FORMAT_BGRx ||
      pwbuf->buffer->n_datas != 1)
    return 0;

  struct spa_data *data = &pwbuf->buffer->datas[0];
  struct spa_chunk *chunk = data->chunk;
  struct mapped_plane mapped = {0};
  if (!chunk || chunk->stride <= 0 ||
      !map_readable_plane(data, &mapped))
    return 0;

  uint32_t width = p->video.size.width < STREAM_CROP_MAX_WIDTH
                       ? p->video.size.width : STREAM_CROP_MAX_WIDTH;
  uint32_t height = p->video.size.height < STREAM_CROP_MAX_HEIGHT
                        ? p->video.size.height : STREAM_CROP_MAX_HEIGHT;
  if (width == 0 || height == 0) {
    release_mapped_planes(&mapped, 1);
    return 0;
  }
  uint64_t row_bytes = (uint64_t)width * 4;
  uint64_t end_offset = (uint64_t)chunk->offset +
                        (uint64_t)(height - 1) * (uint32_t)chunk->stride +
                        row_bytes;
  if ((uint64_t)chunk->stride < row_bytes || end_offset > data->maxsize ||
      end_offset - chunk->offset > chunk->size) {
    release_mapped_planes(&mapped, 1);
    printf("event=stream.frame_crop_error reason=plane_bounds\n");
    fflush(stdout);
    return 0;
  }

  size_t rgb_size = (size_t)width * height * 3;
  uint8_t *rgb = malloc(rgb_size);
  if (!rgb) {
    release_mapped_planes(&mapped, 1);
    printf("event=stream.frame_crop_error reason=out_of_memory\n");
    fflush(stdout);
    return 0;
  }
  const uint8_t *source = mapped.base + chunk->offset;
  for (uint32_t y = 0; y < height; y++) {
    const uint8_t *src_row = source + (size_t)y * (uint32_t)chunk->stride;
    uint8_t *dst = rgb + (size_t)y * width * 3;
    for (uint32_t x = 0; x < width; x++) {
      dst[x * 3] = src_row[x * 4 + 2];
      dst[x * 3 + 1] = src_row[x * 4 + 1];
      dst[x * 3 + 2] = src_row[x * 4];
    }
  }
  release_mapped_planes(&mapped, 1);

  uint8_t header[16] = {'D', 'C', 'P', 'F'};
  header[4] = (uint8_t)(width >> 8);
  header[5] = (uint8_t)width;
  header[6] = (uint8_t)(height >> 8);
  header[7] = (uint8_t)height;
  for (uint32_t i = 0; i < 8; i++)
    header[8 + i] = (uint8_t)(timestamp_ns >> ((7 - i) * 8));
  int result = write_all(p->stream_output_fd, header, sizeof(header));
  if (result == 0) result = write_all(p->stream_output_fd, rgb, rgb_size);
  free(rgb);
  if (result != 0) {
    printf("event=stream.frame_pipe_error errno=%d message=%s\n",
           errno, strerror(errno));
    fflush(stdout);
    return -1;
  }
  p->stream_frames_sent++;
  printf("event=stream.frame_packet_sent index=%" PRIu64
         " width=%u height=%u timestamp_ns=%" PRIu64 "\n",
         frame_index, width, height, timestamp_ns);
  fflush(stdout);
  return 1;
}

static int copy_first_cpu_frame(struct probe *p, struct pw_buffer *pwbuf,
                                uint64_t buffer_number) {
  if (!pwbuf || !pwbuf->buffer) return 0;

  struct spa_buffer *buffer = pwbuf->buffer;
  int readable = buffer->n_datas > 0 && buffer->n_datas <= MAX_PLANES;
  size_t total_bytes = 0;
  struct mapped_plane mapped[MAX_PLANES] = {{0}};
  printf("event=stream.buffer index=%" PRIu64 " data_planes=%u\n",
         buffer_number, buffer->n_datas);

  for (uint32_t i = 0; i < buffer->n_datas && i < MAX_PLANES; i++) {
    struct spa_data *data = &buffer->datas[i];
    struct spa_chunk *chunk = data->chunk;
    uint32_t offset = chunk ? chunk->offset : 0;
    uint32_t size = chunk ? chunk->size : 0;
    int32_t stride = chunk ? chunk->stride : 0;
    int chunk_ok = chunk != NULL && offset <= data->maxsize &&
                   size <= data->maxsize - offset && size > 0 &&
                   size <= MAX_FRAME_BYTES - total_bytes;
    int plane_ok = chunk_ok && map_readable_plane(data, &mapped[i]);
    if (plane_ok) {
      total_bytes += size;
    } else {
      readable = 0;
      if (mapped[i].mapping) {
        munmap(mapped[i].mapping, mapped[i].mapping_size);
        mapped[i] = (struct mapped_plane){0};
      }
    }
    printf("event=stream.buffer_plane index=%u type=%u flags=%u "
           "fd=%" PRId64 " maxsize=%u chunk_offset=%u chunk_size=%u stride=%d "
           "cpu_readable=%s\n",
           i, data->type, data->flags, data->fd, data->maxsize,
           offset, size, stride, plane_ok ? "true" : "false");
  }
  fflush(stdout);

  if (!readable || p->frame_saved) {
    release_mapped_planes(mapped, buffer->n_datas < MAX_PLANES
                                  ? buffer->n_datas : MAX_PLANES);
    return 0;
  }

  struct saved_plane copied[MAX_PLANES] = {{0}};
  for (uint32_t i = 0; i < buffer->n_datas; i++) {
    struct spa_data *data = &buffer->datas[i];
    struct spa_chunk *chunk = data->chunk;
    copied[i].data_index = i;
    copied[i].chunk_offset = chunk->offset;
    copied[i].size = chunk->size;
    copied[i].stride = chunk->stride;
    copied[i].bytes = malloc(chunk->size);
    if (!copied[i].bytes) {
      for (uint32_t j = 0; j < i; j++) free(copied[j].bytes);
      release_mapped_planes(mapped, buffer->n_datas);
      printf("event=stream.frame_copy_error reason=out_of_memory\n");
      fflush(stdout);
      return 0;
    }
    memcpy(copied[i].bytes, mapped[i].base + chunk->offset, chunk->size);
  }
  release_mapped_planes(mapped, buffer->n_datas);

  for (uint32_t i = 0; i < buffer->n_datas; i++) p->planes[i] = copied[i];
  p->plane_count = buffer->n_datas;
  p->first_cpu_buffer = buffer_number;
  p->frame_saved = 1;
  printf("event=stream.first_cpu_frame_copied index=%" PRIu64
         " planes=%u width=%u height=%u\n",
         buffer_number, p->plane_count,
         p->have_format ? p->video.size.width : 0,
         p->have_format ? p->video.size.height : 0);
  fflush(stdout);
  return 1;
}

static void on_process(void *data) {
  struct probe *p = data;
  p->process_callbacks++;
  uint32_t dequeued = 0;
  struct pw_buffer *buffer;
  while ((buffer = pw_stream_dequeue_buffer(p->stream)) != NULL) {
    p->buffers_seen++;
    dequeued++;
    if (p->stream_output_fd >= 0) {
      uint64_t now = monotonic_nanoseconds();
      uint64_t interval = (uint64_t)p->stream_interval_ms * 1000000u;
      if (p->last_stream_attempt_ns == 0 ||
          now - p->last_stream_attempt_ns >= interval) {
        p->last_stream_attempt_ns = now;
        int result = stream_frame_crop(p, buffer, p->buffers_seen, now);
        if (result < 0) {
          p->stream_error = 1;
        }
      }
    } else {
      copy_first_cpu_frame(p, buffer, p->buffers_seen);
    }
    pw_stream_queue_buffer(p->stream, buffer);
  }
  if (p->stream_output_fd < 0) {
    printf("event=stream.process callback=%" PRIu64
           " buffers_dequeued=%u\n",
           p->process_callbacks, dequeued);
    fflush(stdout);
  }
}

static const struct pw_stream_events stream_events = {
  .version = PW_VERSION_STREAM_EVENTS,
  .state_changed = on_state_changed,
  .param_changed = on_param_changed,
  .process = on_process,
};

static int build_format_param(struct probe *p, uint8_t *storage,
                              size_t storage_size, const struct spa_pod **out) {
  struct spa_rectangle sizes[3] = {
    SPA_RECTANGLE(p->requested_width, p->requested_height),
    SPA_RECTANGLE(1, 1),
    SPA_RECTANGLE(8192, 4096),
  };
  struct spa_fraction variable_framerate = SPA_FRACTION(0, 1);
  struct spa_fraction max_framerates[3] = {
    SPA_FRACTION(0, 1), SPA_FRACTION(0, 1), SPA_FRACTION(1000, 1),
  };
  if (p->stream_output_fd >= 0) {
    /* Bound the source stream itself in watch mode. The crop interval only
     * limits CPU-side sampling; without this cap PipeWire still sends every
     * display-rate buffer even when almost all of them are discarded. */
    max_framerates[0] = SPA_FRACTION(STREAM_MAX_FPS, 1);
    max_framerates[1] = SPA_FRACTION(1, 1);
    max_framerates[2] = SPA_FRACTION(STREAM_MAX_FPS, 1);
  }
  struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, storage_size);
  struct spa_pod_frame object;
  spa_pod_builder_push_object(&builder, &object,
                              SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat);
  spa_pod_builder_add(&builder,
      SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
      SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
      SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
      SPA_FORMAT_VIDEO_size,
      SPA_POD_CHOICE_RANGE_Rectangle(&sizes[0], &sizes[1], &sizes[2]),
      SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&variable_framerate),
      SPA_FORMAT_VIDEO_maxFramerate,
      SPA_POD_CHOICE_RANGE_Fraction(&max_framerates[0], &max_framerates[1],
                                    &max_framerates[2]),
      0);
  *out = spa_pod_builder_pop(&builder, &object);
  return *out ? 0 : -1;
}

static int save_capture(const struct probe *p) {
  char raw_path[4096];
  char json_path[4096];
  int raw_len = snprintf(raw_path, sizeof(raw_path), "%s.raw", p->output_prefix);
  int json_len = snprintf(json_path, sizeof(json_path), "%s.json", p->output_prefix);
  if (raw_len < 0 || (size_t)raw_len >= sizeof(raw_path) ||
      json_len < 0 || (size_t)json_len >= sizeof(json_path)) {
    fprintf(stderr, "error=output_path_too_long\n");
    return -1;
  }

  FILE *raw = fopen(raw_path, "wb");
  if (!raw) {
    fprintf(stderr, "error=open_raw path=%s message=%s\n",
            raw_path, strerror(errno));
    return -1;
  }
  int failed = 0;
  for (uint32_t i = 0; i < p->plane_count; i++) {
    if (fwrite(p->planes[i].bytes, 1, p->planes[i].size, raw) != p->planes[i].size) {
      failed = 1;
      break;
    }
  }
  if (fclose(raw) != 0) failed = 1;
  if (failed) {
    fprintf(stderr, "error=write_raw path=%s message=%s\n",
            raw_path, strerror(errno));
    return -1;
  }

  FILE *json = fopen(json_path, "w");
  if (!json) {
    fprintf(stderr, "error=open_metadata path=%s message=%s\n",
            json_path, strerror(errno));
    return -1;
  }
  fprintf(json,
      "{\n"
      "  \"node_id\": %u,\n"
      "  \"buffer_index\": %" PRIu64 ",\n"
      "  \"width\": %u,\n"
      "  \"height\": %u,\n"
      "  \"format_id\": %u,\n"
      "  \"raw_file\": \"%s\",\n"
      "  \"planes\": [\n",
      p->node_id, p->first_cpu_buffer,
      p->have_format ? p->video.size.width : 0,
      p->have_format ? p->video.size.height : 0,
      p->have_format ? p->video.format : 0, raw_path);
  for (uint32_t i = 0; i < p->plane_count; i++) {
    const struct saved_plane *plane = &p->planes[i];
    fprintf(json,
        "    {\"data_index\": %u, \"chunk_offset\": %u, "
        "\"size\": %u, \"stride\": %d}%s\n",
        plane->data_index, plane->chunk_offset, plane->size, plane->stride,
        i + 1 < p->plane_count ? "," : "");
  }
  fprintf(json, "  ]\n}\n");
  int json_error = ferror(json);
  if (fclose(json) != 0) json_error = 1;
  if (json_error) {
    fprintf(stderr, "error=write_metadata path=%s\n", json_path);
    return -1;
  }
  printf("event=stream.frame_saved raw=%s metadata=%s\n", raw_path, json_path);
  fflush(stdout);
  return 0;
}

static int sync_core(struct probe *p, struct pw_core *core,
                     const char *stage, int timeout_ms) {
  int seq = pw_core_sync(core, PW_ID_CORE, 0);
  if (seq < 0) {
    printf("event=core.sync_error stage=%s result=%d\n", stage, seq);
    fflush(stdout);
    return -1;
  }
  p->core_sync_seq = seq;
  p->core_sync_done = 0;
  printf("event=core.sync stage=%s seq=%d\n", stage, seq);
  fflush(stdout);

  double deadline = monotonic_seconds() + (double)timeout_ms / 1000.0;
  while (!p->core_sync_done && monotonic_seconds() < deadline) {
    int result = pw_loop_iterate(pw_main_loop_get_loop(p->main_loop), 100);
    if (result < 0 && result != -EINTR) {
      printf("event=core.sync_iterate_error stage=%s result=%d\n",
             stage, result);
      fflush(stdout);
      return -1;
    }
  }
  if (!p->core_sync_done) {
    printf("event=core.sync_timeout stage=%s seq=%d\n", stage, seq);
    fflush(stdout);
    return -1;
  }
  return 0;
}

static int disconnect_stream_safely(struct probe *p, struct pw_core *core) {
  int result = pw_stream_disconnect(p->stream);
  printf("event=stream.disconnect result=%d\n", result);
  fflush(stdout);

  int unconnected = 0;
  double deadline = monotonic_seconds() + 3.0;
  while (monotonic_seconds() < deadline) {
    enum pw_stream_state state = pw_stream_get_state(p->stream, NULL);
    if (state == PW_STREAM_STATE_UNCONNECTED) {
      unconnected = 1;
      break;
    }
    int iterate_result =
        pw_loop_iterate(pw_main_loop_get_loop(p->main_loop), 100);
    if (iterate_result < 0 && iterate_result != -EINTR) {
      printf("event=stream.disconnect_iterate_error result=%d\n",
             iterate_result);
      fflush(stdout);
      break;
    }
  }
  printf("event=stream.disconnect_state unconnected=%s state=%s\n",
         unconnected ? "true" : "false",
         pw_stream_state_as_string(pw_stream_get_state(p->stream, NULL)));
  fflush(stdout);

  /* The sync barrier waits for the server's buffer-removal work to finish
   * before this client destroys the stream or closes the portal connection. */
  int sync_result = sync_core(p, core, "after_stream_disconnect", 3000);
  return result < 0 || !unconnected || sync_result != 0 ? -1 : 0;
}

static int parse_uint(const char *name, const char *value, uint32_t *out) {
  char *end = NULL;
  errno = 0;
  unsigned long parsed = strtoul(value, &end, 10);
  if (errno || end == value || *end != '\0' || parsed > UINT32_MAX) {
    fprintf(stderr, "error=invalid_argument name=%s value=%s\n", name, value);
    return -1;
  }
  *out = (uint32_t)parsed;
  return 0;
}

static int parse_int(const char *name, const char *value, int *out) {
  char *end = NULL;
  errno = 0;
  long parsed = strtol(value, &end, 10);
  if (errno || end == value || *end != '\0' || parsed < 1 || parsed > 600000) {
    fprintf(stderr, "error=invalid_argument name=%s value=%s\n", name, value);
    return -1;
  }
  *out = (int)parsed;
  return 0;
}

static void usage(const char *program) {
  fprintf(stderr,
      "Usage: %s --fd FD --node-id ID [--width PX --height PX] "
      "[--timeout-ms MS] [--output-prefix PATH] "
      "[--stream-output-fd FD --stream-interval-ms MS]\n", program);
}

int main(int argc, char **argv) {
  int fd = -1;
  uint32_t node_id = UINT32_MAX;
  uint32_t width = 1280;
  uint32_t height = 800;
  int timeout_ms = 15000;
  int stream_output_fd = -1;
  int stream_interval_ms = 500;
  const char *output_prefix = "/tmp/wow-context-bridge-frame";

  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "--fd") == 0 && i + 1 < argc) {
      if (parse_int("--fd", argv[++i], &fd) != 0) return 2;
    } else if (strcmp(argv[i], "--node-id") == 0 && i + 1 < argc) {
      if (parse_uint("--node-id", argv[++i], &node_id) != 0) return 2;
    } else if (strcmp(argv[i], "--width") == 0 && i + 1 < argc) {
      if (parse_uint("--width", argv[++i], &width) != 0 || width == 0) return 2;
    } else if (strcmp(argv[i], "--height") == 0 && i + 1 < argc) {
      if (parse_uint("--height", argv[++i], &height) != 0 || height == 0) return 2;
    } else if (strcmp(argv[i], "--timeout-ms") == 0 && i + 1 < argc) {
      if (parse_int("--timeout-ms", argv[++i], &timeout_ms) != 0) return 2;
    } else if (strcmp(argv[i], "--stream-output-fd") == 0 && i + 1 < argc) {
      if (parse_int("--stream-output-fd", argv[++i], &stream_output_fd) != 0) return 2;
    } else if (strcmp(argv[i], "--stream-interval-ms") == 0 && i + 1 < argc) {
      if (parse_int("--stream-interval-ms", argv[++i], &stream_interval_ms) != 0) return 2;
    } else if (strcmp(argv[i], "--output-prefix") == 0 && i + 1 < argc) {
      output_prefix = argv[++i];
    } else {
      usage(argv[0]);
      return 2;
    }
  }
  if (fd < 0 || node_id == UINT32_MAX || width > 8192 || height > 4096) {
    usage(argv[0]);
    return 2;
  }

  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  if (stream_output_fd >= 0) signal(SIGPIPE, SIG_IGN);
  struct probe probe = {
    .node_id = node_id,
    .timeout_ms = timeout_ms,
    .stream_output_fd = stream_output_fd,
    .stream_interval_ms = stream_interval_ms,
    .requested_width = width,
    .requested_height = height,
    .output_prefix = output_prefix,
  };

  printf("event=probe.start backend=native-libpipewire node_id=%u "
         "portal_selector=numeric-node-id width=%u height=%u timeout_ms=%d "
         "mode=%s max_framerate_fps=%u\n",
         node_id, width, height, timeout_ms,
         stream_output_fd >= 0 ? "stream" : "one-shot",
         stream_output_fd >= 0 ? STREAM_MAX_FPS : 0);
  fflush(stdout);
  pw_init(&argc, &argv);

  probe.main_loop = pw_main_loop_new(NULL);
  if (!probe.main_loop) {
    fprintf(stderr, "error=main_loop_create errno=%d message=%s\n",
            errno, strerror(errno));
    pw_deinit();
    return 2;
  }
  struct pw_context *context =
      pw_context_new(pw_main_loop_get_loop(probe.main_loop), NULL, 0);
  if (!context) {
    fprintf(stderr, "error=context_create errno=%d message=%s\n",
            errno, strerror(errno));
    pw_main_loop_destroy(probe.main_loop);
    pw_deinit();
    return 2;
  }

  int pipewire_fd = dup(fd);
  if (pipewire_fd < 0) {
    fprintf(stderr, "error=fd_dup errno=%d message=%s\n",
            errno, strerror(errno));
    pw_context_destroy(context);
    pw_main_loop_destroy(probe.main_loop);
    pw_deinit();
    return 2;
  }
  struct pw_core *core = pw_context_connect_fd(context, pipewire_fd, NULL, 0);
  if (!core) {
    int saved_errno = errno;
    close(pipewire_fd);
    fprintf(stderr, "error=context_connect_fd errno=%d message=%s\n",
            saved_errno, strerror(saved_errno));
    pw_context_destroy(context);
    pw_main_loop_destroy(probe.main_loop);
    pw_deinit();
    return 2;
  }
  printf("event=core.connected fd=%d portal_fd=%d\n", pipewire_fd, fd);
  fflush(stdout);
  pw_core_add_listener(core, &probe.core_listener, &core_events, &probe);

  struct pw_properties *properties = pw_properties_new(
      PW_KEY_MEDIA_TYPE, "Video",
      PW_KEY_MEDIA_CATEGORY, "Capture",
      PW_KEY_MEDIA_ROLE, "Screen",
      NULL);
  probe.stream = pw_stream_new(core, "Decktation Native Portal Probe", properties);
  if (!probe.stream) {
    fprintf(stderr, "error=stream_create errno=%d message=%s\n",
            errno, strerror(errno));
    pw_core_disconnect(core);
    pw_context_destroy(context);
    pw_main_loop_destroy(probe.main_loop);
    pw_deinit();
    return 2;
  }
  pw_stream_add_listener(probe.stream, &probe.stream_listener,
                         &stream_events, &probe);

  uint8_t format_storage[2048];
  const struct spa_pod *format = NULL;
  if (build_format_param(&probe, format_storage, sizeof(format_storage),
                         &format) != 0) {
    fprintf(stderr, "error=format_parameter_build\n");
    pw_stream_destroy(probe.stream);
    pw_core_disconnect(core);
    pw_context_destroy(context);
    pw_main_loop_destroy(probe.main_loop);
    pw_deinit();
    return 2;
  }
  const struct spa_pod *params[] = {format};
  int connect_result = pw_stream_connect(
      probe.stream, PW_DIRECTION_INPUT, node_id,
      PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS, params, 1);
  printf("event=stream.connect node_id=%u result=%d\n", node_id, connect_result);
  fflush(stdout);
  if (connect_result < 0) probe.connect_error = 1;

  double deadline = monotonic_seconds() + (double)timeout_ms / 1000.0;
  while ((probe.stream_output_fd >= 0 || !probe.frame_saved) &&
         !probe.stream_error && !probe.connect_error && !stop_requested &&
         monotonic_seconds() < deadline) {
    int result = pw_loop_iterate(pw_main_loop_get_loop(probe.main_loop), 100);
    if (result < 0) {
      if (stop_requested && result == -EINTR) break;
      printf("event=loop.error result=%d\n", result);
      probe.stream_error = 1;
    }
  }

  int save_result = 0;
  if (probe.frame_saved) {
    save_result = save_capture(&probe);
  } else if (stop_requested) {
    printf("event=probe.interrupted\n");
  } else if (!probe.stream_error && !probe.connect_error) {
    printf("event=probe.timeout buffers_seen=%" PRIu64
           " process_callbacks=%" PRIu64 "\n",
           probe.buffers_seen, probe.process_callbacks);
  }
  printf("event=probe.summary frame_saved=%s stream_frames_sent=%" PRIu64
         " buffers_seen=%" PRIu64 " process_callbacks=%" PRIu64
         " stream_error=%s connect_error=%s\n",
         probe.frame_saved ? "true" : "false", probe.stream_frames_sent,
         probe.buffers_seen, probe.process_callbacks,
         probe.stream_error ? "true" : "false",
         probe.connect_error ? "true" : "false");
  fflush(stdout);

  int teardown_error = disconnect_stream_safely(&probe, core);
  spa_hook_remove(&probe.stream_listener);
  pw_stream_destroy(probe.stream);
  if (sync_core(&probe, core, "after_stream_destroy", 3000) != 0)
    teardown_error = 1;
  spa_hook_remove(&probe.core_listener);
  pw_core_disconnect(core);
  pw_context_destroy(context);
  pw_main_loop_destroy(probe.main_loop);
  pw_deinit();
  for (uint32_t i = 0; i < probe.plane_count; i++) free(probe.planes[i].bytes);
  printf("event=probe.teardown status=%s\n",
         teardown_error ? "incomplete" : "complete");
  fflush(stdout);
  return (save_result != 0 || probe.connect_error || probe.stream_error ||
          teardown_error) ? 2 : 0;
}
