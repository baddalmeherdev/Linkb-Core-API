import yt_dlp
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


@app.route("/")
def health_check():
    return jsonify({"status": "Active", "message": "Linkb API is running"})


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()

    if not url:
        return jsonify({"success": False, "error": "No URL provided"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "noplaylist": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "Unknown Title")
        thumbnail = info.get("thumbnail", "")
        download_url = info.get("url") or info.get("manifest_url", "")

        if not download_url:
            # Fallback: pick the best mp4 format url from formats list
            formats = info.get("formats", [])
            mp4_formats = [
                f for f in formats
                if f.get("ext") == "mp4" and f.get("url")
            ]
            if mp4_formats:
                best = max(mp4_formats, key=lambda f: f.get("height") or 0)
                download_url = best["url"]

        if not download_url:
            return jsonify({"success": False, "error": "Could not extract a direct download URL"}), 422

        return jsonify({
            "success": True,
            "title": title,
            "thumbnail": thumbnail,
            "download_url": download_url,
        })

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"success": False, "error": f"Unsupported or invalid URL: {str(e)}"}), 422
    except yt_dlp.utils.ExtractorError as e:
        return jsonify({"success": False, "error": f"Extraction failed: {str(e)}"}), 422
    except Exception as e:
        return jsonify({"success": False, "error": f"An unexpected error occurred: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
