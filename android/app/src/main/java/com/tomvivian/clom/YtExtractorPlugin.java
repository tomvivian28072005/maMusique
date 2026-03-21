package com.tomvivian.clom;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import org.schabi.newpipe.extractor.NewPipe;
import org.schabi.newpipe.extractor.ServiceList;
import org.schabi.newpipe.extractor.stream.StreamInfo;
import org.schabi.newpipe.extractor.stream.AudioStream;
import org.schabi.newpipe.extractor.stream.StreamType;
import org.schabi.newpipe.extractor.downloader.Downloader;
import org.schabi.newpipe.extractor.downloader.Request;
import org.schabi.newpipe.extractor.downloader.Response;
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;

import java.io.IOException;
import java.io.File;
import java.io.FileOutputStream;
import java.util.List;
import java.util.Map;

import java.net.HttpURLConnection;
import java.net.URL;
import java.io.InputStream;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;

@CapacitorPlugin(name = "YtExtractor")
public class YtExtractorPlugin extends Plugin {

    private static boolean initialized = false;

    private void ensureInit() {
        if (!initialized) {
            NewPipe.init(new AndroidDownloader());
            initialized = true;
        }
    }

    @PluginMethod
    public void extract(PluginCall call) {
        String url = call.getString("url");
        if (url == null || url.isEmpty()) {
            call.reject("URL manquante");
            return;
        }

        new Thread(() -> {
            try {
                ensureInit();
                StreamInfo info = StreamInfo.getInfo(ServiceList.YouTube, url);
                AudioStream best = pickBestAudio(info);
                if (best == null) { call.reject("Aucun flux audio trouvé"); return; }

                JSObject result = new JSObject();
                result.put("audioUrl", best.getUrl());
                result.put("title", info.getName());
                result.put("artist", info.getUploaderName());
                result.put("thumbnailUrl", info.getThumbnails().isEmpty() ? "" :
                    info.getThumbnails().get(info.getThumbnails().size() - 1).getUrl());
                result.put("duration", info.getDuration());
                result.put("mimeType", best.getFormat() != null ? best.getFormat().getMimeType() : "audio/mp4");
                result.put("extension", best.getFormat() != null ? best.getFormat().getSuffix() : "m4a");
                call.resolve(result);
            } catch (Exception e) {
                call.reject("Erreur extraction: " + e.getMessage(), e);
            }
        }).start();
    }

    /**
     * Télécharge un morceau YouTube : extraction audio + download fichier + thumbnail.
     * Tout se fait côté natif pour éviter les problèmes CORS du WebView.
     */
    @PluginMethod
    public void download(PluginCall call) {
        String url = call.getString("url");
        if (url == null || url.isEmpty()) {
            call.reject("URL manquante");
            return;
        }

        new Thread(() -> {
            try {
                ensureInit();

                // 1. Extraire les infos
                StreamInfo info = StreamInfo.getInfo(ServiceList.YouTube, url);
                AudioStream best = pickBestAudio(info);
                if (best == null) { call.reject("Aucun flux audio trouvé"); return; }

                String title = info.getName();
                String artist = info.getUploaderName();
                String ext = (best.getFormat() != null) ? best.getFormat().getSuffix() : "m4a";

                // 2. Préparer les dossiers dans le stockage externe de l'app
                File baseDir = getContext().getExternalFilesDir(null);
                File dlDir = new File(baseDir, "Clom/downloads");
                File coverDir = new File(baseDir, "Clom/covers");
                dlDir.mkdirs();
                coverDir.mkdirs();

                // 3. Télécharger le fichier audio
                String safeName = title.replaceAll("[^a-zA-Z0-9àâéèêëïîôùûüÿçæœÀÂÉÈÊËÏÎÔÙÛÜŸÇÆŒ _-]", "");
                if (safeName.length() > 80) safeName = safeName.substring(0, 80);
                String fileName = safeName + "_" + System.currentTimeMillis() + "." + ext;
                File audioFile = new File(dlDir, fileName);

                downloadFile(best.getUrl(), audioFile);

                // 4. Télécharger la thumbnail
                String coverFileName = null;
                String thumbnailUrl = info.getThumbnails().isEmpty() ? "" :
                    info.getThumbnails().get(info.getThumbnails().size() - 1).getUrl();
                if (!thumbnailUrl.isEmpty()) {
                    try {
                        coverFileName = "track_yt_" + System.currentTimeMillis() + ".jpg";
                        File coverFile = new File(coverDir, coverFileName);
                        File tempCover = new File(coverDir, "temp_" + coverFileName);
                        downloadFile(thumbnailUrl, tempCover);
                        cropToSquare(tempCover, coverFile);
                        tempCover.delete();
                    } catch (Exception e) {
                        coverFileName = null;
                    }
                }

                // 5. Retourner les résultats
                JSObject result = new JSObject();
                result.put("title", title);
                result.put("artist", artist);
                result.put("filePath", "/downloads/" + fileName);
                result.put("coverPath", coverFileName != null ? "/covers/" + coverFileName + "?t=" + System.currentTimeMillis() : "");
                result.put("youtubeUrl", url);
                result.put("duration", info.getDuration());
                call.resolve(result);

            } catch (Exception e) {
                call.reject("Erreur téléchargement: " + e.getMessage(), e);
            }
        }).start();
    }

    @PluginMethod
    public void search(PluginCall call) {
        String query = call.getString("query");
        if (query == null || query.isEmpty()) {
            call.reject("Requête manquante");
            return;
        }

        new Thread(() -> {
            try {
                ensureInit();
                var extractor = ServiceList.YouTube.getSearchExtractor(query);
                extractor.fetchPage();

                var items = extractor.getInitialPage().getItems();
                org.json.JSONArray results = new org.json.JSONArray();
                int count = 0;
                for (var item : items) {
                    if (count >= 10) break;
                    if (item instanceof org.schabi.newpipe.extractor.stream.StreamInfoItem) {
                        var si = (org.schabi.newpipe.extractor.stream.StreamInfoItem) item;
                        if (si.getStreamType() == StreamType.VIDEO_STREAM || si.getStreamType() == StreamType.AUDIO_STREAM) {
                            org.json.JSONObject obj = new org.json.JSONObject();
                            obj.put("url", si.getUrl());
                            obj.put("title", si.getName());
                            obj.put("artist", si.getUploaderName());
                            obj.put("duration", si.getDuration());
                            obj.put("thumbnailUrl", si.getThumbnails().isEmpty() ? "" : si.getThumbnails().get(0).getUrl());
                            results.put(obj);
                            count++;
                        }
                    }
                }

                JSObject result = new JSObject();
                result.put("results", results);
                call.resolve(result);
            } catch (Exception e) {
                call.reject("Erreur recherche: " + e.getMessage(), e);
            }
        }).start();
    }

    // ── Helpers ──────────────────────────────────────────────

    private AudioStream pickBestAudio(StreamInfo info) {
        List<AudioStream> streams = info.getAudioStreams();
        if (streams == null || streams.isEmpty()) return null;
        AudioStream best = null;
        int bestBitrate = 0;
        for (AudioStream s : streams) {
            int br = s.getAverageBitrate();
            if (br > bestBitrate) { bestBitrate = br; best = s; }
        }
        return best != null ? best : streams.get(0);
    }

    /**
     * Télécharge un fichier par chunks (Range requests) pour éviter le throttling YouTube.
     * YouTube coupe la connexion après ~1Mo si on ne fait pas de range requests.
     */
    private void downloadFile(String urlStr, File dest) throws IOException {
        final int CHUNK_SIZE = 2 * 1024 * 1024; // 2 Mo par chunk
        final int MAX_RETRIES = 3;

        // D'abord, obtenir la taille totale
        long totalSize = -1;
        try {
            HttpURLConnection head = (HttpURLConnection) new URL(urlStr).openConnection();
            head.setRequestMethod("HEAD");
            setDownloadHeaders(head);
            head.setInstanceFollowRedirects(true);
            totalSize = head.getContentLengthLong();
            head.disconnect();
        } catch (Exception e) {
            // Si HEAD échoue, on essaie quand même en téléchargement direct
        }

        try (FileOutputStream out = new FileOutputStream(dest)) {
            if (totalSize > 0) {
                // Téléchargement par chunks avec Range
                long downloaded = 0;
                while (downloaded < totalSize) {
                    long end = Math.min(downloaded + CHUNK_SIZE - 1, totalSize - 1);
                    boolean success = false;
                    for (int retry = 0; retry < MAX_RETRIES && !success; retry++) {
                        HttpURLConnection conn = null;
                        try {
                            conn = (HttpURLConnection) new URL(urlStr).openConnection();
                            setDownloadHeaders(conn);
                            conn.setRequestProperty("Range", "bytes=" + downloaded + "-" + end);
                            conn.setInstanceFollowRedirects(true);

                            try (InputStream in = conn.getInputStream()) {
                                byte[] buf = new byte[8192];
                                int n;
                                while ((n = in.read(buf)) != -1) {
                                    out.write(buf, 0, n);
                                    downloaded += n;
                                }
                            }
                            success = true;
                        } catch (IOException e) {
                            if (retry == MAX_RETRIES - 1) throw e;
                            try { Thread.sleep(1000 * (retry + 1)); } catch (InterruptedException ie) {}
                        } finally {
                            if (conn != null) conn.disconnect();
                        }
                    }
                }
            } else {
                // Fallback : téléchargement direct si on ne connaît pas la taille
                HttpURLConnection conn = (HttpURLConnection) new URL(urlStr).openConnection();
                setDownloadHeaders(conn);
                conn.setInstanceFollowRedirects(true);
                try (InputStream in = conn.getInputStream()) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                    }
                } finally {
                    conn.disconnect();
                }
            }
        }
    }

    /**
     * Recadre une image en carré (crop centré) et sauvegarde en JPEG.
     */
    private void cropToSquare(File input, File output) throws IOException {
        Bitmap bmp = BitmapFactory.decodeFile(input.getAbsolutePath());
        if (bmp == null) throw new IOException("Impossible de décoder l'image");
        int w = bmp.getWidth(), h = bmp.getHeight();
        int size = Math.min(w, h);
        int x = (w - size) / 2, y = (h - size) / 2;
        Bitmap cropped = Bitmap.createBitmap(bmp, x, y, size, size);
        try (FileOutputStream out = new FileOutputStream(output)) {
            cropped.compress(Bitmap.CompressFormat.JPEG, 90, out);
        }
        if (cropped != bmp) cropped.recycle();
        bmp.recycle();
    }

    private void setDownloadHeaders(HttpURLConnection conn) {
        conn.setRequestProperty("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36");
        conn.setRequestProperty("Accept", "*/*");
        conn.setRequestProperty("Accept-Language", "en-US,en;q=0.9");
        conn.setRequestProperty("Origin", "https://www.youtube.com");
        conn.setRequestProperty("Referer", "https://www.youtube.com/");
        conn.setConnectTimeout(15000);
        conn.setReadTimeout(30000);
    }

    /**
     * Downloader HTTP pour NewPipe Extractor.
     */
    private static class AndroidDownloader extends Downloader {
        @Override
        public Response execute(Request request) throws IOException, ReCaptchaException {
            HttpURLConnection conn = (HttpURLConnection) new URL(request.url()).openConnection();
            conn.setRequestMethod(request.httpMethod());
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(20000);
            conn.setInstanceFollowRedirects(true);

            for (Map.Entry<String, List<String>> entry : request.headers().entrySet()) {
                for (String val : entry.getValue()) {
                    conn.setRequestProperty(entry.getKey(), val);
                }
            }
            conn.setRequestProperty("User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36");
            conn.setRequestProperty("Cookie", "CONSENT=YES+cb.20210328-17-p0.en+FX+435");

            byte[] body = request.dataToSend();
            if (body != null && body.length > 0) {
                conn.setDoOutput(true);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body);
                }
            }

            int code = conn.getResponseCode();
            if (code == 429) {
                throw new ReCaptchaException("Rate limited", request.url());
            }

            InputStream is = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
            StringBuilder sb = new StringBuilder();
            if (is != null) {
                try (BufferedReader br = new BufferedReader(new InputStreamReader(is))) {
                    String line;
                    while ((line = br.readLine()) != null) {
                        sb.append(line).append('\n');
                    }
                }
            }

            Map<String, List<String>> responseHeaders = conn.getHeaderFields();
            String latestUrl = conn.getURL().toString();
            return new Response(code, conn.getResponseMessage(), responseHeaders, sb.toString(), latestUrl);
        }
    }
}
