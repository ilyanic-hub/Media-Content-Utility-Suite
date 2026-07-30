import io
import re
from typing import Optional
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
import httpx
from bs4 import BeautifulSoup
from pathlib import Path

app = FastAPI(title="Media Utility Suite")

# HTML шаблоны
templates = Jinja2Templates(directory="templates")
# Получаем абсолютный путь к папке с проектом
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ---------------------------------------------------------
# 1. КОНВЕРТАЦИЯ, СЖАТИЕ И УДАЛЕНИЕ EXIF (Идеи 1 и 3)
# ---------------------------------------------------------
@app.post("/api/process-image")
async def process_image(
    file: UploadFile = File(...),
    target_format: str = Form("WEBP"),  # WEBP, JPEG, PNG
    quality: int = Form(80),            # 1-100
    strip_exif: bool = Form(True),      # Очистка EXIF
    max_width: Optional[int] = Form(None)
):
    try:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents))

        # Исправляем ориентацию на основе EXIF перед его удалением
        try:
            image = ImageOps.exif_transpose(image)
        except Exception:
            pass

        # Если делаем очистку EXIF — создаем новый холст без метаданных
        if strip_exif:
            clean_image = Image.new(image.mode, image.size)
            clean_image.putdata(list(image.getdata()))
            image = clean_image

        # Ресайз при необходимости
        if max_width and image.width > max_width:
            aspect_ratio = max_width / float(image.width)
            new_height = int(float(image.height) * aspect_ratio)
            image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)

        # Конвертация RGBA в RGB для JPEG
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
# 2. ОЧИСТКА И ОБРАБОТКА ТЕКСТА (Идея 3)
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
# 3. ПАРСИНГ И ИЗВЛЕЧЕНИЕ МЕДИА ПО ССЫЛКЕ (Идея 4)
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

    # Мета-данные
    title = soup.title.string if soup.title else ""
    og_image = soup.find("meta", property="og:image")
    og_image_url = og_image["content"] if og_image and "content" in og_image.attrs else None

    # Поиск всех картинок
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

    # Удаляем дубликаты картинок
    unique_images = list(dict.fromkeys(images))

    return {
        "title": title,
        "main_image": og_image_url,
        "total_images": len(unique_images),
        "images": unique_images[:30]  # Показываем первые 30
    }
