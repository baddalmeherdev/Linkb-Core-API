import logging
import yt_dlp
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Constants ──────────────────────────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":         "en-US,en;q=0.9",
    "Accept-Encoding":         "gzip, deflate, br",
    "DNT":                     "1",
    "Connection":              "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── CRITICAL: Pre-merged format string ────────────────────────────────────────
# We intentionally avoid bestvideo+bestaudio because that requires ffmpeg
# to merge on the server. Instead we only request formats where one file
# already contains BOTH a video stream AND an audio stream.
#
# The 18/22 fallbacks are legacy YouTube format IDs that are always
# pre-merged: 18 = 360p mp4+aac, 22 = 720p mp4+aac.
PREMERGED_FORMAT = "/".join([
    "best[vcodec!=none][acodec!=none][ext=mp4]",   # Best pre-merged mp4
    "best[vcodec!=none][acodec!=none]",             # Best pre-merged any ext
    "22",                                           # YouTube 720p mp4 (pre-merged)
    "18",                                           # YouTube 360p mp4 (pre-merged)
    "best",                                         # Absolute fallback
])

QUALITY_LABELS = {
    2160: "4K",
    1440: "2K",
    1080: "1080p",
    720:  "720p",
    480:  "480p",
    360:  "360p",
    240:  "240p",
    144:  "144p",
}


# ── Helpers ────────────────────────────────────────────────────────────────────
def label_for_height(height: int | None) -> str:
    """Snap a raw pixel height to the nearest standard quality label."""
    if not height:
        return "SD"
    for threshold, label in sorted(QUALITY_LABELS.items(), reverse=True):
        if height >= threshold:
            return label
    return f"{height}p"


def is_premerged(f: dict) -> bool:
    """
    Return True only when a format carries BOTH a video stream and an
    audio stream in the same file — i.e. no ffmpeg merge is required.
    """
    has_video = f.get("vcodec", "none") not in ("none", None, "")
    has_audio = f.get("acodec", "none") not in ("none", None, "")
    has_url   = bool(f.get("url"))
    return has_video and has_audio and has_url


def extract_best_premerged_url(info: dict) -> str:
    """
    Return the URL of the best pre-merged (video+audio) format.
    Never returns a video-only or audio-only stream.

    Priority:
      1. Root-level url  — if the extractor already resolved one combined stream
      2. Best mp4 combined from formats list  (highest height)
      3. Best combined any-ext from formats list
      4. Empty string — caller must handle this as a failure
    """
    formats = info.get("formats", [])

    # Step 1 — root url: only trust it when root-level codecs confirm audio+video
    root_vcodec = info.get("vcodec", "none")
    root_acodec = info.get("acodec", "none")
    root_url    = info.get("url", "")
    if (
        root_url
        and root_vcodec not in ("none", None, "")
        and root_acodec not in ("none", None, "")
    ):
        logger.info("Best URL: root-level combined stream.")
        return root_url

    if not formats:
        logger.warning("Best URL: formats list is empty.")
        return ""

    premerged = [f for f in formats if is_premerged(f)]
    logger.info(
        "Best URL: %d pre-merged formats out of %d total.",
        len(premerged), len(formats),
    )

    if not premerged:
        logger.error("Best URL: zero pre-merged formats found — cannot return audio-safe URL.")
        return ""

    # Step 2 — best mp4 combined
    mp4 = [f for f in premerged if f.get("ext") == "mp4"]
    if mp4:
        best = max(mp4, key=lambda f: f.get("height") or 0)
        logger.info("Best URL: mp4 combined — id=%s h=%s", best.get("format_id"), best.get("height"))
        return best["url"]

    # Step 3 — best combined any ext
    best = max(premerged, key=lambda f: f.get("height") or 0)
    logger.info("Best URL: any-ext combined — id=%s ext=%s h=%s",
                best.get("format_id"), best.get("ext"), best.get("height"))
    return best["url"]


def build_format_list(info: dict) -> list[dict]:
    """
    Build a deduplicated quality-picker list containing ONLY pre-merged
    (video+audio) formats. Video-only and audio-only entries are always
    excluded to prevent silent playback in the Android app.

    Deduplication key: (quality_label, ext)
    Tiebreaker: highest tbr (total bitrate) wins within each bucket.
    Sort order: best quality first (height desc), combined-only.
    """
    formats = info.get("formats", [])
    if not formats:
        return []

    seen: dict[str, dict] = {}

    for f in formats:
        if not is_premerged(f):
            continue                        # Hard skip — no audio = not allowed

        height   = f.get("height")
        ext      = f.get("ext", "mp4")
        tbr      = f.get("tbr") or 0
        filesize = f.get("filesize") or f.get("filesize_approx")
        label    = label_for_height(height)
        key      = f"{label}|{ext}"

        if key not in seen or tbr > (seen[key].get("tbr") or 0):
            seen[key] = {
                "quality_label": label,
                "height":        height,
                "ext":           ext,
                "has_audio":     True,      # Guaranteed by is_premerged()
                "filesize":      filesize,
                "format_id":     f.get("format_id"),
                "tbr":           tbr,
                "url":           f["url"],
            }

    if not seen:
        logger.warning("Format list: no pre-merged formats found for quality picker.")
        return []

    results = sorted(seen.values(), key=lambda f: f.get("height") or 0, reverse=True)

    logger.info(
        "Format list: %d unique pre-merged qualities — %s",
        len(results),
        [r["quality_label"] for r in results],
    )

    return [
        {
            "quality_label": r["quality_label"],
            "height":        r["height"],
            "ext":           r["ext"],
            "has_audio":     True,
            "filesize":      r["filesize"],
            "url":           r["url"],
        }
        for r in results
    ]


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def health_check():
    """UptimeRobot pings this every 5 min to keep Render's free tier alive."""
    return jsonify({"status": "Active", "message": "Linkb API is running"})


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()

    if not url:
        logger.warning("Request received with no URL parameter.")
        return jsonify({"success": False, "error": "Missing required query parameter: url"}), 400

    logger.info("=== New request: %s ===", url)

    ydl_opts = {
        # ── Core ─────────────────────────────────────────────────────────
        "quiet":         True,
        "no_warnings":   False,
        "skip_download": True,          # Never write bytes to disk
        "noplaylist":    True,

        # ── Pre-merged only format string ────────────────────────────────
        "format": PREMERGED_FORMAT,

        # ── Bot-bypass headers ───────────────────────────────────────────
        "http_headers": BROWSER_HEADERS,

        # ── Anti-bot extractor args ──────────────────────────────────────
        #
        # YouTube-specific:
        #   • "android_vr" / "android" clients serve pre-merged mp4 streams
        #     at resolutions up to 1080p — no ffmpeg needed, no 422 errors.
        #   • "ios" similarly serves pre-merged streams reliably.
        #   • Explicitly skipping "web" avoids the adaptive (DASH) streams
        #     that are video-only or audio-only and would require merging.
        #
        # Instagram/TikTok:
        #   • Both use a single combined mp4 CDN URL so no special args needed.
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"],
                "skip":          ["hls", "dash"],   # Avoid manifest streams
            },
        },

        # ── Robustness ───────────────────────────────────────────────────
        "socket_timeout":   30,
        "retries":           3,
        "fragment_retries":  3,
        "ignoreerrors":     False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Unwrap playlist shell that leaks through noplaylist on some URLs
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries", []) if e]
            if not entries:
                return jsonify({"success": False, "error": "Playlist is empty or inaccessible."}), 422
            info = entries[0]
            logger.info("Unwrapped playlist — using first entry.")

        # ── Metadata ──────────────────────────────────────────────────────
        title       = info.get("title")         or "Unknown Title"
        thumbnail   = info.get("thumbnail")     or ""
        description = info.get("description")   or ""
        duration    = info.get("duration")       or None
        platform    = info.get("extractor_key") or info.get("extractor") or "unknown"
        uploader    = info.get("uploader")       or info.get("channel") or ""
        view_count  = info.get("view_count")     or None
        like_count  = info.get("like_count")     or None

        # ── Primary URL: strictly pre-merged ─────────────────────────────
        download_url = extract_best_premerged_url(info)

        if not download_url:
            logger.error(
                "No pre-merged URL found. Platform=%s. Info keys: %s",
                platform, list(info.keys()),
            )
            return jsonify({
                "success": False,
                "error": (
                    f"No pre-merged (audio+video) stream found for this {platform} URL. "
                    "The content may be DRM-protected, private, or geo-restricted."
                ),
            }), 422

        # ── Quality picker ────────────────────────────────────────────────
        format_list = build_format_list(info)

        # If the format list came back empty (e.g. platform uses a single
        # combined stream with no explicit formats array), synthesise one
        # entry from the best URL we already found so the UI is never empty.
        if not format_list and download_url:
            format_list = [{
                "quality_label": label_for_height(info.get("height")),
                "height":        info.get("height"),
                "ext":           info.get("ext", "mp4"),
                "has_audio":     True,
                "filesize":      info.get("filesize") or info.get("filesize_approx"),
                "url":           download_url,
            }]
            logger.info("Format list: synthesised single entry from root info.")

        logger.info(
            "=== Done — platform=%s | title=%s | formats=%d ===",
            platform, title, len(format_list),
        )

        return jsonify({
            "success":      True,
            "platform":     platform,
            "title":        title,
            "description":  description,
            "thumbnail":    thumbnail,
            "duration":     duration,
            "uploader":     uploader,
            "view_count":   view_count,
            "like_count":   like_count,
            "download_url": download_url,   # Best pre-merged stream
            "formats":      format_list,    # Quality picker — all have audio
        })

    # ── yt-dlp exceptions ──────────────────────────────────────────────────────
    except yt_dlp.utils.DownloadError as e:
        logger.error("DownloadError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Download error: {str(e)}"}), 422

    except yt_dlp.utils.ExtractorError as e:
        logger.error("ExtractorError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Extractor error: {str(e)}"}), 422

    except yt_dlp.utils.GeoRestrictionError as e:
        logger.error("GeoRestrictionError: %s", str(e))
        return jsonify({
            "success": False,
            "error": "Content is geo-restricted and cannot be accessed from this server's region.",
        }), 451

    except yt_dlp.utils.UserNotLive as e:
        logger.error("UserNotLive: %s", str(e))
        return jsonify({"success": False, "error": "This live stream is not currently active."}), 422

    except yt_dlp.utils.UnsupportedError as e:
        logger.error("UnsupportedError: %s", str(e))
        return jsonify({"success": False, "error": f"Unsupported URL or platform: {str(e)}"}), 422

    except yt_dlp.utils.PostProcessingError as e:
        logger.error("PostProcessingError: %s", str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Post-processing error: {str(e)}"}), 500

    except Exception as e:
        logger.error("Unhandled exception for %s: %s", url, str(e), exc_info=True)
        return jsonify({"success": False, "error": f"Unexpected server error: {str(e)}"}), 500


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
