import os
import re
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests as std_requests
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Manhwa Downloader Web App")

# إنشاء المجلدات المطلوبة لتفادي الأخطاء
os.makedirs("downloads", exist_ok=True)
os.makedirs("templates", exist_ok=True)

templates = Jinja2Templates(directory="templates")

# قاموس تتبع حالة المهام الشغالة
tasks = {}


def download_task_worker(
    task_id: str, manhwa_url: str, start_chapter: int, max_chapters: int | None
):
    """دالة التنزيل والرفع التي تعمل في الخلفية"""
    try:
        tasks[task_id] = {
            "status": "running",
            "message": "جاري تجهيز الاتصال بالسيرفر...",
            "progress": 5,
            "download_url": None,
        }

        series_name = manhwa_url.rstrip("/").split("/")[-1]
        session = curl_requests.Session(impersonate="chrome120")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://google.com",
        }

        current_ch = start_chapter
        downloaded_count = 0
        current_batch_files = []
        total_expected = max_chapters if max_chapters else 20

        while True:
            if max_chapters is not None and downloaded_count >= max_chapters:
                break

            url = f"{manhwa_url}/{current_ch}"
            cbz_filename = os.path.join(
                "downloads", f"{series_name}_Ch_{current_ch:03d}.cbz"
            )

            # تحديث نسبة التقدم للمستخدم
            calc_prog = min(
                90, int((downloaded_count / total_expected) * 85) + 10
            )
            tasks[task_id]["progress"] = calc_prog
            tasks[task_id][
                "message"
            ] = f"جاري تحميل الفصل {current_ch} (تم تحميل {downloaded_count}/{max_chapters or '∞'})..."

            # 1. سحب صفحة الفصل
            try:
                response = session.get(url, headers=headers, timeout=20)
            except Exception as e:
                print(f"[-] Fetch Error: {e}")
                break

            if response.status_code != 200:
                print(f"[-] HTTP Status: {response.status_code}")
                break

            soup = BeautifulSoup(response.text, "html.parser")
            img_tags = soup.select(
                ".reader-area img, .reading-content img, #chapter_imgs img, .rdimg img, .entry-content img"
            )
            image_urls = []
            for img in img_tags:
                src = (
                    img.get("src")
                    or img.get("data-src")
                    or img.get("data-lazy-src")
                )
                if src:
                    src = src.strip()
                    if src.startswith("//"):
                        src = "https:" + src
                    image_urls.append(src)

            if not image_urls:
                matches = re.findall(
                    r'https?://[^\s"\\]+\.(?:jpg|jpeg|png|webp)', response.text
                )
                image_urls = list(dict.fromkeys(matches))

            if not image_urls:
                break

            # 2. تنزيل الصور بالتوازي
            downloaded_images = []

            def download_single(idx, img_url):
                try:
                    res = session.get(img_url, headers=headers, timeout=15)
                    if res.status_code == 200:
                        ext = img_url.split(".")[-1].split("?")[0].lower()
                        if ext not in ["jpg", "jpeg", "png", "webp"]:
                            ext = "jpg"
                        return f"{idx:03d}.{ext}", res.content
                except Exception:
                    pass
                return None, None

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(download_single, idx, u)
                    for idx, u in enumerate(image_urls, start=1)
                ]
                for f in as_completed(futures):
                    fn, cnt = f.result()
                    if fn and cnt:
                        downloaded_images.append((fn, cnt))

            downloaded_images.sort(key=lambda x: x[0])

            if not downloaded_images:
                break

            # حفظ الفصل كملف CBZ
            with zipfile.ZipFile(
                cbz_filename, "w", zipfile.ZIP_DEFLATED
            ) as cbz:
                for fn, cnt in downloaded_images:
                    cbz.writestr(fn, cnt)

            current_batch_files.append(cbz_filename)
            downloaded_count += 1
            current_ch += 1
            time.sleep(1)

        if not current_batch_files:
            tasks[task_id]["status"] = "failed"
            tasks[task_id][
                "message"
            ] = "فشل تحميل الفصول. تأكد من صحة الرابط ورقم الفصل."
            return

        # 3. ضغط الفصول في ملف ZIP واحد
        start_ch = start_chapter
        end_ch = current_ch - 1
        zip_name = f"{series_name}_Ch_{start_ch:03d}_to_{end_ch:03d}.zip"
        zip_path = os.path.join("downloads", zip_name)

        tasks[task_id]["progress"] = 92
        tasks[task_id]["message"] = "جاري تجميع وضغط الفصول..."

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in current_batch_files:
                if os.path.exists(file_path):
                    zipf.write(file_path, os.path.basename(file_path))
                    os.remove(file_path)  # تنظيف الملفات الفرعية

        # 4. الرفع على PixelDrain واستخراج "رابط التحميل المباشر"
        tasks[task_id]["progress"] = 96
        tasks[task_id][
            "message"
        ] = "جاري الرفع لإنشاء رابط تحميل مباشر وسريع..."

        download_link = None
        try:
            with open(zip_path, "rb") as f:
                res = std_requests.post(
                    "https://pixeldrain.com/api/file",
                    files={"file": f},
                    timeout=600,
                )
            if res.status_code in [200, 201]:
                file_id = res.json().get("id")
                # 💡 استخدام رابط التحميل المباشر مع بارامتر ?download
                download_link = f"https://pixeldrain.com/api/file/{file_id}?download"
        except Exception as e:
            print(f"PixelDrain Upload Error: {e}")

        # الخطة البديلة: التحميل المباشر من سيرفر الموقع إذا فشل الرفع الخارجي
        if not download_link:
            download_link = f"/download-file/{zip_name}"

        tasks[task_id]["status"] = "completed"
        tasks[task_id]["progress"] = 100
        tasks[task_id][
            "message"
        ] = "تم اكتمال التجهيز! سيبدأ التحميل المباشر الآن..."
        tasks[task_id]["download_url"] = download_link

    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["message"] = f"حدث خطأ غير متوقع: {str(e)}"


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/start-download")
async def start_download(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    start_chapter: int = Form(1),
    max_chapters: str = Form(""),
):
    num_chapters = int(max_chapters) if max_chapters.isdigit() else None
    task_id = str(uuid.uuid4())

    tasks[task_id] = {
        "status": "pending",
        "message": "جاري إطلاق العملية في الخلفية...",
        "progress": 0,
        "download_url": None,
    }

    background_tasks.add_task(
        download_task_worker, task_id, url.strip(), start_chapter, num_chapters
    )

    return {"task_id": task_id}


@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return JSONResponse(
            status_code=404, content={"message": "المهمة غير موجودة"}
        )
    return task


@app.get("/download-file/{filename}")
async def download_file(filename: str):
    file_path = os.path.join("downloads", filename)
    if os.path.exists(file_path):
        return FileResponse(
            file_path, filename=filename, media_type="application/zip"
        )
    return JSONResponse(status_code=404, content={"message": "الملف غير موجود"})
