from typing import Optional
from fastapi import HTTPException
import fitz


class PDFEncryptedException(Exception):
    """Levantada quando um PDF está protegido por senha."""
    def __init__(self, filename: str):
        self.filename = filename
        super().__init__(f"PDF is password protected: {filename}")


async def extract_text_from_file(file_bytes: bytes, filename: str, password: str | None = None) -> str:
    """Extrai texto de arquivos .pdf, .csv e .txt.

    - .pdf: usa PyMuPDF (fitz). Se protegido por senha, tenta autenticar.
    - .csv / .txt: tenta decodificar em utf-8, cai para latin-1 em falha

    Lança PDFEncryptedException se o PDF estiver protegido por senha.
    Lança HTTPException(status_code=400) para formatos inválidos ou arquivos corrompidos.
    """
    if not filename or "." not in filename:
        raise HTTPException(status_code=400, detail="Formato de arquivo não suportado ou corrompido.")

    ext = filename.rsplit(".", 1)[-1].lower()

    if ext == "pdf":
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception:
            raise HTTPException(status_code=400, detail="Formato de arquivo não suportado ou corrompido.")

        try:
            if doc.is_encrypted:
                if password:
                    if not doc.authenticate(password):
                        raise PDFEncryptedException(filename)
                else:
                    raise PDFEncryptedException(filename)
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text("text"))
            return "\n".join(text_parts)
        finally:
            try:
                doc.close()
            except Exception:
                pass

    elif ext in {"csv", "txt"}:
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return file_bytes.decode("latin-1")
            except Exception:
                raise HTTPException(status_code=400, detail="Formato de arquivo não suportado ou corrompido.")

    else:
        raise HTTPException(status_code=400, detail="Formato de arquivo não suportado ou corrompido.")
