"""
Camada de armazenamento de arquivos (uploads) baseada em MongoDB GridFS.

Motivação:
- Armazena todos os arquivos enviados dentro do próprio MongoDB (bucket "uploads_fs"),
  eliminando escrita em disco local do pod (que é efêmero em ambientes hospedados).
- Funciona identicamente no preview Emergent e no VPS do usuário — basta o Mongo estar
  acessível (o que já é o caso).
- `filename` é o identificador opaco usado pelo restante do sistema (mesmo campo salvo
  no schema `cadastros.documento_frente/verso.filename`).
"""

from __future__ import annotations
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorGridFSBucket, AsyncIOMotorDatabase


_bucket: Optional[AsyncIOMotorGridFSBucket] = None
_db: Optional[AsyncIOMotorDatabase] = None


def init(db: AsyncIOMotorDatabase) -> None:
    """Chamado uma vez no startup para configurar o GridFS bucket."""
    global _bucket, _db
    _db = db
    _bucket = AsyncIOMotorGridFSBucket(db, bucket_name='uploads_fs')


def _get_bucket() -> AsyncIOMotorGridFSBucket:
    if _bucket is None:
        raise RuntimeError("storage.init(db) não foi chamado no startup")
    return _bucket


async def save_bytes(filename: str, data: bytes, content_type: str = 'application/octet-stream') -> None:
    """Salva bytes no GridFS sob o nome `filename` (deve ser único)."""
    bucket = _get_bucket()
    # Se já existir um arquivo com esse nome, remove antes para não acumular versões.
    async for existing in bucket.find({'filename': filename}):
        try:
            await bucket.delete(existing['_id'])
        except Exception:
            pass
    await bucket.upload_from_stream(
        filename,
        data,
        metadata={'contentType': content_type},
    )


async def read_bytes(filename: str) -> Optional[bytes]:
    """Retorna os bytes do arquivo salvo, ou None se não existir."""
    bucket = _get_bucket()
    try:
        stream = await bucket.open_download_stream_by_name(filename)
    except Exception:
        return None
    try:
        return await stream.read()
    finally:
        try:
            await stream.close()
        except Exception:
            pass


async def file_exists(filename: str) -> bool:
    bucket = _get_bucket()
    async for _ in bucket.find({'filename': filename}).limit(1):
        return True
    return False


async def delete_file(filename: str) -> bool:
    """Remove o arquivo do GridFS (todas as revisões com esse nome)."""
    bucket = _get_bucket()
    removed = False
    async for doc in bucket.find({'filename': filename}):
        try:
            await bucket.delete(doc['_id'])
            removed = True
        except Exception:
            pass
    return removed
