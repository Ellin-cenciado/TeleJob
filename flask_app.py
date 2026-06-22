from dotenv import load_dotenv
import os

import time
import requests
from flask import Flask, request, jsonify
from google import genai
import threading

# Diccionarios globales para agrupar las fotos de los álbumes
MEDIA_GROUPS = {}
MEDIA_TIMERS = {}
GROUPS_LOCK = threading.Lock()

app = Flask(__name__)

carpeta_actual = os.path.dirname(os.path.abspath(__file__))
ruta_env = os.path.join(carpeta_actual, '.env')
load_dotenv(ruta_env, override=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
LINKEDIN_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Versión de la nueva API de LinkedIn (formato YYYYMM)
LINKEDIN_VERSION = "202601"

# Proxy requerido por PythonAnywhere para conexiones salientes
PROXIES = {
    "http": "http://proxy.server:3128",
    "https": "http://proxy.server:3128",
}

ai_client = genai.Client(api_key=GEMINI_API_KEY)


def _linkedin_headers(content_type="application/json"):
    """Headers requeridos por la nueva API versionada de LinkedIn."""
    return {
        "Authorization": f"Bearer {LINKEDIN_TOKEN}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-RestLi-Protocol-Version": "2.0.0",
        "Content-Type": content_type,
    }


def procesar_album(media_group_id):
    """Función en segundo plano que se ejecuta cuando Telegram termina de enviar el álbum."""
    with GROUPS_LOCK:
        grupo = MEDIA_GROUPS.pop(media_group_id, None)
        MEDIA_TIMERS.pop(media_group_id, None)

    if not grupo:
        return

    chat_id = grupo['chat_id']
    caption = grupo['caption']
    file_ids = grupo['file_ids']

    try:
        # 1. Descargar todas las imágenes del álbum
        images_bytes_list = []
        for file_id in file_ids:
            img_bytes = descargar_foto_telegram(file_id)
            images_bytes_list.append(img_bytes)
            time.sleep(1)

        # 2. Generar texto con Gemini
        linkedin_text = generar_texto_linkedin(images_bytes_list, caption)

        # 3. Publicar en LinkedIn
        publicar_en_linkedin(images_bytes_list, linkedin_text)

        enviar_mensaje_confirmacion(
            chat_id,
            f"✅ ¡Álbum de {len(images_bytes_list)} fotos publicado con éxito en LinkedIn!"
        )

    except Exception as e:
        print(f"Error en el pipeline del álbum: {str(e)}")
        enviar_mensaje_confirmacion(chat_id, f"🚨 Hubo un error procesando tu álbum: {str(e)}")


@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    payload = request.get_json()

    if not payload or 'message' not in payload or 'photo' not in payload['message']:
        return jsonify({"status": "ok", "message": "No se detectó ninguna foto"}), 200

    message = payload['message']
    chat_id = message['chat']['id']
    caption = message.get('caption', '')

    photo_info = message['photo'][-1]
    file_id = photo_info['file_id']

    # ¿ES PARTE DE UN ÁLBUM?
    if 'media_group_id' in message:
        media_group_id = message['media_group_id']

        with GROUPS_LOCK:
            if media_group_id not in MEDIA_GROUPS:
                MEDIA_GROUPS[media_group_id] = {
                    'chat_id': chat_id,
                    'caption': '',
                    'file_ids': []
                }

            MEDIA_GROUPS[media_group_id]['file_ids'].append(file_id)

            if caption:
                MEDIA_GROUPS[media_group_id]['caption'] = caption

        if media_group_id in MEDIA_TIMERS:
            MEDIA_TIMERS[media_group_id].cancel()

        t = threading.Timer(3.5, procesar_album, args=[media_group_id])
        MEDIA_TIMERS[media_group_id] = t
        t.start()

        return jsonify({"status": "ok", "message": "Foto agrupada"}), 200

    # --- CASO DE UNA SOLA FOTO ---
    try:
        image_bytes = descargar_foto_telegram(file_id)
        linkedin_text = generar_texto_linkedin([image_bytes], caption)
        publicar_en_linkedin([image_bytes], linkedin_text)
        enviar_mensaje_confirmacion(chat_id, "✅ ¡Post publicado con éxito en LinkedIn!")
    except Exception as e:
        print(f"Error en el pipeline: {str(e)}")
        enviar_mensaje_confirmacion(chat_id, f"🚨 Hubo un error procesando tu post: {str(e)}")

    return jsonify({"status": "ok"}), 200


def descargar_foto_telegram(file_id, retries=3):
    get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"

    for attempt in range(retries):
        try:
            res = requests.get(get_file_url, timeout=10, proxies=PROXIES).json()

            if not res.get("ok"):
                raise Exception(f"Error obteniendo file_path: {res}")

            file_path = res["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

            img_res = requests.get(download_url, timeout=10, proxies=PROXIES)
            if img_res.status_code == 200:
                return img_res.content
            else:
                raise Exception(f"Fallo descargando la imagen. HTTP: {img_res.status_code}")

        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < retries - 1:
                print(f"Intento {attempt + 1} falló por error de red. Reintentando en 2 segundos...")
                time.sleep(2)
            else:
                raise Exception(f"Falló tras {retries} intentos. Error final: {str(e)}")


def generar_texto_linkedin(images_bytes_list, caption):
    prompt = (
        "Analiza la siguiente imagen y redacta una publicación para LinkedIn de alta calidad. "
        "El tono debe ser el de un profesional entusiasta que comparte un logro, un aprendizaje "
        "o un proyecto técnico relevante para su red de contactos y reclutadores. "
        "Usa un léxico corporativo moderno en español argentino moderado, estructúralo en 2 o 3 párrafos cortos para facilitar la lectura, "
        "y añade entre 3 y 5 hashtags estratégicos al final. No incluyas textos introductorios, solo el post final."
        "Aparte de eso, incluye su traduccion en Ingles al final del texto para demostrar alto conocimiento de ingles."
        "Evitar el uso de caracteres especiales o markdown."
    )
    if caption:
        prompt += (
            f"\n\nIMPORTANTE: El autor ha dado el siguiente contexto: '{caption}'. "
            "Redacta el post basándote en esta idea principal y amplíala usando un tono corporativo pero cercano."
        )

    contents = [prompt]
    for img_bytes in images_bytes_list:
        contents.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": img_bytes
            }
        })

    response = ai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=contents
    )
    return response.text


def publicar_en_linkedin(images_bytes_list, linkedin_text):
    # --- 1. Obtener el URN del usuario ---
    user_info_res = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}"},
        proxies=PROXIES
    )
    user_info = user_info_res.json()

    if 'sub' in user_info:
        author_urn = f"urn:li:person:{user_info['sub']}"
    elif 'id' in user_info:
        author_urn = f"urn:li:person:{user_info['id']}"
    else:
        raise Exception(f"Fallo de Auth en LinkedIn: {user_info}")

    # --- 2. Subir imágenes con la NUEVA API /rest/images ---
    # (El endpoint /v2/assets?action=registerUpload fue deprecado por LinkedIn)
    image_urns = []

    for i, img_bytes in enumerate(images_bytes_list):
        # Paso A: Inicializar la subida para obtener la URL temporal
        init_res = requests.post(
            "https://api.linkedin.com/rest/images?action=initializeUpload",
            json={"initializeUploadRequest": {"owner": author_urn}},
            headers=_linkedin_headers(),
            proxies=PROXIES
        )

        if init_res.status_code not in [200, 201]:
            raise Exception(
                f"Error al inicializar subida de imagen {i + 1}: "
                f"[{init_res.status_code}] {init_res.text}"
            )

        init_data = init_res.json()
        if 'value' not in init_data:
            raise Exception(
                f"Respuesta inesperada de LinkedIn al inicializar imagen {i + 1}: {init_data}"
            )

        upload_url = init_data['value']['uploadUrl']
        image_urn = init_data['value']['image']

        # Paso B: Subir el binario al upload_url
        upload_res = requests.put(
            upload_url,
            data=img_bytes,
            headers={
                "Authorization": f"Bearer {LINKEDIN_TOKEN}",
                "Content-Type": "image/jpeg"
            },
            proxies=PROXIES
        )

        # LinkedIn devuelve 201 al subir una imagen exitosamente
        if upload_res.status_code not in [200, 201]:
            raise Exception(
                f"Error al subir imagen {i + 1}: "
                f"[{upload_res.status_code}] {upload_res.text}"
            )

        image_urns.append(image_urn)
        print(f"Imagen {i + 1}/{len(images_bytes_list)} subida: {image_urn}")

    # --- 3. Construir el contenido multimedia del post ---
    if len(image_urns) == 1:
        content = {
            "media": {
                "altText": "Imagen de la publicación",
                "id": image_urns[0]
            }
        }
    else:
        content = {
            "multiImage": {
                "images": [
                    {"altText": f"Imagen {j + 1}", "id": urn}
                    for j, urn in enumerate(image_urns)
                ]
            }
        }

    # --- 4. Crear el post con la NUEVA API /rest/posts ---
    post_payload = {
        "author": author_urn,
        "commentary": linkedin_text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "content": content,
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False
    }

    post_res = requests.post(
        "https://api.linkedin.com/rest/posts",
        json=post_payload,
        headers=_linkedin_headers(),
        proxies=PROXIES
    )

    if post_res.status_code not in [200, 201]:
        raise Exception(
            f"Error al crear el post en LinkedIn: "
            f"[{post_res.status_code}] {post_res.text}"
        )


def enviar_mensaje_confirmacion(chat_id, texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": texto}, proxies=PROXIES)


if __name__ == '__main__':
    app.run(port=5000)
