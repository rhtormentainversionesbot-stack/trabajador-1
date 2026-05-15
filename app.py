"""
TRABAJADOR 1 — Backend Flask
Sistema de generación automática de vídeos para YouTube
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import google.generativeai as genai
from elevenlabs.client import ElevenLabs
from elevenlabs import save
import requests
import os
import json
import uuid
import threading
from moviepy.editor import (
    ImageClip, AudioFileClip, CompositeVideoClip,
    TextClip, concatenate_videoclips
)
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

app = Flask(__name__)
CORS(app)

# ============================================================
# CLAVES — ponlas en variables de entorno en Render
# ============================================================
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
PEXELS_API_KEY     = os.environ.get("PEXELS_API_KEY", "")
YOUTUBE_TOKEN      = os.environ.get("YOUTUBE_TOKEN", "")

genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.0-flash")
eleven = ElevenLabs(api_key=ELEVENLABS_API_KEY)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
JOBS = {}  # almacén de trabajos en memoria

# ============================================================
# UTILIDADES
# ============================================================
def generar_guion(tema, instrucciones_extra=""):
    prompt = (
        "Eres un analista financiero experto para el canal YouTube Inversion Rapida. "
        "Crea un guion en espanol de exactamente 50 segundos sobre: " + tema + ". "
        + (("Instrucciones adicionales: " + instrucciones_extra + ". ") if instrucciones_extra else "") +
        "ESTRUCTURA: 1) Gancho impactante 5 segundos. "
        "2) Analisis: precio, por que invertir o vender, potencial. 35 segundos. "
        "3) Cierre motivador con llamada a suscribirse. 10 segundos. "
        "REGLAS: Maximo 120 palabras. Directo, confiado. "
        "Incluye al final: Esto no es asesoramiento financiero. "
        "Solo el texto para narrar, sin acotaciones ni titulos."
    )
    r = gemini.generate_content(prompt)
    return r.text.strip()

def generar_titulo(tema):
    prompt = (
        "Crea un titulo viral para YouTube sobre: " + tema + ". "
        "Maximo 60 caracteres. Sin emojis. En espanol. Solo el titulo, nada mas."
    )
    r = gemini.generate_content(prompt)
    return r.text.strip()

def generar_descripcion(tema, guion):
    prompt = (
        "Crea una descripcion para YouTube sobre: " + tema + ". "
        "Basate en este guion: " + guion[:300] + ". "
        "Maximo 200 palabras. Incluye hashtags relevantes de bolsa. En espanol."
    )
    r = gemini.generate_content(prompt)
    return r.text.strip() + "\n\nEsto no es asesoramiento financiero."

def obtener_imagenes(query, job_id, cantidad=5):
    headers = {"Authorization": PEXELS_API_KEY}
    url = "https://api.pexels.com/v1/search?query=" + query + " stock market finance&per_page=" + str(cantidad) + "&orientation=landscape"
    r = requests.get(url, headers=headers)
    data = r.json()
    rutas = []
    os.makedirs("/tmp/" + job_id, exist_ok=True)
    for i, foto in enumerate(data.get("photos", [])):
        img_url = foto["src"]["large"]
        ruta = "/tmp/" + job_id + "/img_" + str(i) + ".jpg"
        img_data = requests.get(img_url).content
        with open(ruta, "wb") as f:
            f.write(img_data)
        rutas.append(ruta)
    return rutas

def generar_voz(texto, job_id):
    ruta = "/tmp/" + job_id + "/audio.mp3"
    audio = eleven.generate(
        text=texto,
        voice="Rachel",
        model="eleven_multilingual_v2"
    )
    save(audio, ruta)
    return ruta

def montar_video(imagenes, audio_path, guion, job_id):
    salida = "/tmp/" + job_id + "/video_final.mp4"
    audio = AudioFileClip(audio_path)
    duracion_total = audio.duration
    duracion_img = duracion_total / len(imagenes)

    clips = []
    for ruta in imagenes:
        clip = ImageClip(ruta).set_duration(duracion_img).resize(height=1080)
        clips.append(clip)

    video = concatenate_videoclips(clips, method="compose")
    video = video.set_audio(audio)

    palabras = guion.split()
    bloques = [" ".join(palabras[i:i+6]) for i in range(0, len(palabras), 6)]
    dur_bloque = duracion_total / max(len(bloques), 1)

    subtitulos = []
    for i, bloque in enumerate(bloques):
        txt = (TextClip(bloque, fontsize=52, color="white", font="Arial-Bold",
                        stroke_color="black", stroke_width=2,
                        method="caption", size=(1700, None))
               .set_start(i * dur_bloque)
               .set_duration(dur_bloque)
               .set_position(("center", 0.82), relative=True))
        subtitulos.append(txt)

    final = CompositeVideoClip([video] + subtitulos)
    final.write_videofile(salida, fps=30, codec="libx264",
                          audio_codec="aac", verbose=False, logger=None)
    return salida

def subir_youtube(video_path, titulo, descripcion):
    creds_data = json.loads(YOUTUBE_TOKEN)
    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": titulo,
            "description": descripcion,
            "tags": ["bolsa", "acciones", "inversion", "finanzas", "acciones baratas", "invertir"],
            "categoryId": "22"
        },
        "status": {"privacyStatus": "public"}
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = req.execute()
    return "https://youtube.com/watch?v=" + response["id"]

def pipeline(job_id, tema, instrucciones_extra=""):
    try:
        JOBS[job_id]["status"] = "generando_guion"
        guion = generar_guion(tema, instrucciones_extra)
        titulo = generar_titulo(tema)
        descripcion = generar_descripcion(tema, guion)
        JOBS[job_id]["guion"] = guion
        JOBS[job_id]["titulo"] = titulo
        JOBS[job_id]["descripcion"] = descripcion

        JOBS[job_id]["status"] = "descargando_imagenes"
        imagenes = obtener_imagenes(tema, job_id)

        JOBS[job_id]["status"] = "generando_voz"
        audio = generar_voz(guion, job_id)

        JOBS[job_id]["status"] = "montando_video"
        video_path = montar_video(imagenes, audio, guion, job_id)
        JOBS[job_id]["video_path"] = video_path
        JOBS[job_id]["status"] = "listo"

    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)

# ============================================================
# RUTAS API
# ============================================================
@app.route("/api/generar", methods=["POST"])
def api_generar():
    data = request.json
    tema = data.get("tema", "")
    instrucciones = data.get("instrucciones", "")
    if not tema:
        return jsonify({"error": "Falta el tema"}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status": "iniciando", "tema": tema}

    t = threading.Thread(target=pipeline, args=(job_id, tema, instrucciones))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})

@app.route("/api/estado/<job_id>")
def api_estado(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job no encontrado"}), 404
    return jsonify(job)

@app.route("/api/video/<job_id>")
def api_video(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "listo":
        return jsonify({"error": "Video no listo"}), 404
    return send_file(job["video_path"], mimetype="video/mp4")

@app.route("/api/subir/<job_id>", methods=["POST"])
def api_subir(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "listo":
        return jsonify({"error": "Video no listo"}), 404
    try:
        url = subir_youtube(job["video_path"], job["titulo"], job["descripcion"])
        JOBS[job_id]["status"] = "subido"
        JOBS[job_id]["youtube_url"] = url
        return jsonify({"url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/regenerar/<job_id>", methods=["POST"])
def api_regenerar(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job no encontrado"}), 404
    data = request.json
    instrucciones = data.get("instrucciones", "")
    tema = job["tema"]
    JOBS[job_id] = {"status": "iniciando", "tema": tema}
    t = threading.Thread(target=pipeline, args=(job_id, tema, instrucciones))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/")
def index():
    return "TRABAJADOR 1 activo"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
