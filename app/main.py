from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader, PdfWriter


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UPLOADS_DIR = DATA_DIR / "uploads"

ALLOWED_EXT = {"pdf", "doc", "docx"}


def _safe_name(name: str) -> str:
    name = name.strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120] or "file"


def _run(cmd: list[str], *, timeout_s: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"Command not found: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=500, detail="Operation timed out") from e
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or "").strip()
        raise HTTPException(status_code=500, detail=msg or "Command failed") from e


def list_printers() -> list[str]:
    # lpstat -p -> "printer NAME is ..."
    cp = _run(["lpstat", "-p"], timeout_s=10)
    printers: list[str] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line.startswith("printer "):
            continue
        parts = line.split()
        if len(parts) >= 2:
            printers.append(parts[1])
    return sorted(set(printers))


def convert_to_pdf(input_path: Path, out_dir: Path) -> Path:
    # LibreOffice headless conversion
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "libreoffice",
            "--headless",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(input_path),
        ],
        timeout_s=180,
    )
    # LibreOffice output name is based on input basename
    pdf_path = out_dir / f"{input_path.stem}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=500, detail="Convert failed: PDF not produced")
    return pdf_path


def parse_page_range(page_range: str, total: int) -> list[int]:
    """Parse a page-range string like '1,3,5-7' into sorted 1-indexed page numbers."""
    pages: set[int] = set()
    for part in page_range.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            halves = part.split("-", 1)
            try:
                start, end = int(halves[0].strip()), int(halves[1].strip())
                for p in range(start, end + 1):
                    if 1 <= p <= total:
                        pages.add(p)
            except ValueError:
                pass
        else:
            try:
                p = int(part)
                if 1 <= p <= total:
                    pages.add(p)
            except ValueError:
                pass
    return sorted(pages)


def _build_reordered_pdf(original_pdf: Path, output_pdf: Path, pages: list[int]) -> None:
    """Write a new PDF containing only the given 1-indexed pages (in order)."""
    reader = PdfReader(str(original_pdf))
    writer = PdfWriter()
    for page_num in pages:
        writer.add_page(reader.pages[page_num - 1])
    with open(str(output_pdf), "wb") as fh:
        writer.write(fh)


def print_file(printer: str, file_path: Path, copies: int = 1) -> str:
    if copies < 1 or copies > 99:
        raise HTTPException(status_code=400, detail="Invalid copies")
    args = ["lp", "-d", printer, "-n", str(copies), str(file_path)]
    cp = _run(args, timeout_s=30)
    # e.g. "request id is PRINTER-123 (1 file(s))"
    return (cp.stdout or "").strip()


app = FastAPI(title="print-web", version="0.1.0")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "printers": [],
            "file_id": None,
            "file_name": None,
            "error": None,
        },
    )


@app.get("/printers", response_class=JSONResponse)
def printers():
    return {"printers": list_printers()}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename")
    ext = (Path(file.filename).suffix or "").lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="Only PDF/DOC/DOCX allowed")

    file_id = uuid.uuid4().hex
    safe = _safe_name(Path(file.filename).name)

    job_dir = UPLOADS_DIR / file_id
    job_dir.mkdir(parents=True, exist_ok=True)

    src_path = job_dir / f"source_{safe}"
    with src_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    if ext == "pdf":
        pdf_path = job_dir / "document.pdf"
        shutil.copy2(src_path, pdf_path)
    else:
        converted = convert_to_pdf(src_path, job_dir)
        pdf_path = job_dir / "document.pdf"
        shutil.move(str(converted), str(pdf_path))

    return RedirectResponse(url=f"/preview/{file_id}", status_code=303)


@app.get("/preview/{file_id}", response_class=HTMLResponse)
def preview(file_id: str, request: Request):
    job_dir = UPLOADS_DIR / file_id
    pdf_path = job_dir / "document.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return templates.TemplateResponse(
        "preview.html",
        {
            "request": request,
            "file_id": file_id,
        },
    )


@app.get("/files/{file_id}.pdf")
def get_pdf(file_id: str):
    pdf_path = UPLOADS_DIR / file_id / "document.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(pdf_path), media_type="application/pdf", filename="document.pdf")


@app.post("/print")
def do_print(
    file_id: str = Form(...),
    printer: str = Form(...),
    copies: int = Form(1),
):
    available = set(list_printers())
    if printer not in available:
        raise HTTPException(status_code=400, detail="Printer not found in CUPS")
    pdf_path = UPLOADS_DIR / file_id / "document.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    result = print_file(printer, pdf_path, copies=copies)
    return {"ok": True, "result": result}


@app.post("/reorder-and-print")
def reorder_and_print(
    file_id: str = Form(...),
    printer: str = Form(...),
    copies: int = Form(1),
    page_order: str = Form("[]"),
    page_range: str = Form("all"),
    orientation: str = Form("portrait"),
    pages_per_sheet: int = Form(1),
    color_mode: str = Form("color"),
):
    available = set(list_printers())
    if printer not in available:
        raise HTTPException(status_code=400, detail="Printer not found in CUPS")

    job_dir = UPLOADS_DIR / file_id
    original_pdf = job_dir / "document.pdf"
    if not original_pdf.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if copies < 1 or copies > 99:
        raise HTTPException(status_code=400, detail="Invalid copies")

    # Parse and validate page order
    try:
        order: list[int] = json.loads(page_order)
        if not isinstance(order, list):
            order = []
    except (json.JSONDecodeError, ValueError):
        order = []

    reader = PdfReader(str(original_pdf))
    total_pages = len(reader.pages)
    natural_order = list(range(1, total_pages + 1))

    # Sanitise: keep only valid 1-based page numbers
    order = [p for p in order if isinstance(p, int) and 1 <= p <= total_pages]
    if not order:
        order = natural_order

    # Determine which pages to include
    if page_range.strip().lower() == "all":
        pages_to_print = order
    else:
        range_indices = parse_page_range(page_range, len(order))
        pages_to_print = [order[i - 1] for i in range_indices if 1 <= i <= len(order)]
        if not pages_to_print:
            raise HTTPException(status_code=400, detail="Page range produced no pages")

    # Build a processed PDF only when needed
    is_natural = pages_to_print == natural_order
    if is_natural:
        pdf_to_print = original_pdf
    else:
        reordered_pdf = job_dir / "document_reordered.pdf"
        _build_reordered_pdf(original_pdf, reordered_pdf, pages_to_print)
        pdf_to_print = reordered_pdf

    # Build lp command
    args = ["lp", "-d", printer, "-n", str(copies)]

    # Orientation: 3 = portrait, 4 = landscape
    if orientation == "landscape":
        args += ["-o", "orientation-requested=4"]
    else:
        args += ["-o", "orientation-requested=3"]

    # Pages per sheet
    if pages_per_sheet in (2, 4):
        args += ["-o", f"number-up={pages_per_sheet}"]

    # Color mode
    if color_mode == "grayscale":
        args += ["-o", "ColorModel=Gray"]

    args.append(str(pdf_to_print))
    cp = _run(args, timeout_s=30)
    return {"ok": True, "result": (cp.stdout or "").strip()}


@app.get("/health")
def health():
    return {"ok": True}

