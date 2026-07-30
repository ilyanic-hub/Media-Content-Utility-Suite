import io
import re
from pathlib import Path
from typing import Optional
import zipfile
from urllib.parse import urlparse
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
import httpx
from bs4 import BeautifulSoup

app = FastAPI(title="Media Utility Suite")

# Абсолютный путь к папке templates
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# Заглушка для favicon.ico (убирает 404 в консоли)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


# Главная страница
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="index.html"
    )


# ---------------------------------------------------------
# 1. КОНВЕРТАЦИЯ, СЖАТИЕ И УДАЛЕНИЕ EXIF
# ---------------------------------------------------------
@app.post("/api/process-image")
async def process_image(
    file: UploadFile = File(...),
    target_format: str = Form("WEBP"),
    quality: int = Form(80),
    strip_exif: bool = Form(True),
    max_width: Optional[int] = Form(None)
):
    try:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents))

        try:
            image = ImageOps.exif_transpose(image)
        except Exception:
            pass

        if strip_exif:
            clean_image = Image.new(image.mode, image.size)
            clean_image.putdata(list(image.getdata()))
            image = clean_image

        if max_width and image.width > max_width:
            aspect_ratio = max_width / float(image.width)
            new_height = int(float(image.height) * aspect_ratio)
            image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)

        if target_format.upper() == "JPEG" and image.mode in ("RGBA", "P"):
            image = image.convert("RGB")

        output_io = io.BytesIO()
        fmt = target_format.upper()
        if fmt == "JPG":
            fmt = "JPEG"

        save_kwargs = {"format": fmt}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = quality
            save_kwargs["optimize"] = True

        image.save(output_io, **save_kwargs)
        output_io.seek(0)

        mime_types = {
            "WEBP": "image/webp",
            "JPEG": "image/jpeg",
            "PNG": "image/png"
        }

        ext = fmt.lower() if fmt != "JPEG" else "jpg"
        filename = f"processed_{file.filename.rsplit('.', 1)[0]}.{ext}"

        return StreamingResponse(
            output_io,
            media_type=mime_types.get(fmt, "application/octet-stream"),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка обработки изображения: {str(e)}")


# ---------------------------------------------------------
# 2. ОЧИСТКА ТЕКСТА
# ---------------------------------------------------------
@app.post("/api/clean-text")
async def clean_text(
    text: str = Form(...),
    remove_html: bool = Form(True),
    remove_extra_spaces: bool = Form(True),
    remove_duplicate_lines: bool = Form(False)
):
    cleaned = text

    if remove_html:
        cleaned = re.sub(r'<[^>]+>', '', cleaned)

    if remove_extra_spaces:
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        cleaned = re.sub(r'\n\s*\n', '\n\n', cleaned).strip()

    if remove_duplicate_lines:
        lines = cleaned.splitlines()
        seen = set()
        unique_lines = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                unique_lines.append(line)
        cleaned = "\n".join(unique_lines)

    return {"original_length": len(text), "cleaned_length": len(cleaned), "result": cleaned}


# ---------------------------------------------------------
# 3. ПАРСИНГ МЕДИА ПО ССЫЛКЕ
# ---------------------------------------------------------
@app.post("/api/extract-media")
async def extract_media(url: str = Form(...)):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Не удалось загрузить страницу: {str(e)}")

    soup = BeautifulSoup(response.text, "html.parser")

    title = soup.title.string if soup.title else ""
    og_image = soup.find("meta", property="og:image")
    og_image_url = og_image["content"] if og_image and "content" in og_image.attrs else None

    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                from urllib.parse import urljoin
                src = urljoin(url, src)
            if src.startswith("http"):
                images.append(src)

    unique_images = list(dict.fromkeys(images))

    return {
        "title": title,
        "main_image": og_image_url,
        "total_images": len(unique_images),
        "images": unique_images[:30]
    }

# СКАЧИВАНИЕ ИЗОБРАЖЕНИЙ В ZIP
# ---------------------------------------------------------
@app.post("/api/download-zip")
async def download_zip(urls: str = Form(...)):
    # urls передаются строкой через запятую или переносы
    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    if not url_list:
        raise HTTPException(status_code=400, detail="Список URL пуст")

    zip_buffer = io.BytesIO()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for i, img_url in enumerate(url_list, start=1):
                try:
                    resp = await client.get(img_url, headers=headers)
                    if resp.status_code == 200:
                        # Пытаемся забрать оригинальное имя файла или создаем image_N
                        parsed = urlparse(img_url)
                        filename = Path(parsed.path).name
                        if not filename or "." not in filename:
                            ext = resp.headers.get("content-type", "").split("/")[-1]
                            ext = "jpg" if ext not in ["png", "webp", "gif"] else ext
                            filename = f"image_{i}.{ext}"

                        zip_file.writestr(filename, resp.content)
                except Exception:
                    continue  # Пропускаем, если картинка не скачалась

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="parsed_images.zip"'}
