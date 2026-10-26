-- What the size policy needs to know about a file's main video stream (bit depth, HDR type, Dolby Vision profile, video bitrate, scan type), as JSON from the worker's ffprobe.
ALTER TABLE library_file ADD COLUMN video_info_json TEXT;
