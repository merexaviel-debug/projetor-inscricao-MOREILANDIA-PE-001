from fastapi import FastAPI, APIRouter, UploadFile, File, Form, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid, secrets, json
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

# Admin/tracking routes
from admin_routes import admin_router, set_db, seed_admin
set_db(db)

# Storage (GridFS-based)
import storage
storage.init(db)

# (Site público removido — apenas painel admin permanece)


class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatusCheckCreate(BaseModel):
    client_name: str


@api_router.get("/")
async def root():
    return {"message": "Painel Administrativo API"}


@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(**input.model_dump())
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    await db.status_checks.insert_one(doc)
    return status_obj


@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    return status_checks


# ===== Proxies para evitar CORS/CSP no frontend estático =====
import httpx
from fastapi import HTTPException as _dummy_httpex  # noqa: F401

@api_router.get("/cep/{cep}")
async def lookup_cep(cep: str):
    cep_clean = ''.join(c for c in cep if c.isdigit())
    if len(cep_clean) != 8:
        raise HTTPException(status_code=400, detail="CEP inválido")
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"https://viacep.com.br/ws/{cep_clean}/json/")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="ViaCEP indisponível")
        data = r.json()
        if data.get('erro'):
            raise HTTPException(status_code=404, detail="CEP não encontrado")
        return data
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Erro ao consultar ViaCEP: {e}")


@api_router.get("/ibge/municipios/{uf}")
async def lookup_municipios(uf: str):
    uf = uf.strip().upper()
    if len(uf) != 2:
        raise HTTPException(status_code=400, detail="UF inválida")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="IBGE indisponível")
        return r.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Erro ao consultar IBGE: {e}")


app.include_router(admin_router)


# ------------------------------------------------------------------
# Submit público de inscrição (integra com collections do painel admin)
# ------------------------------------------------------------------

# Mapa cargo_codigo → (valor, taxa_str) — Concurso Moreilândia-PE Edital 001/2026
CARGO_VALOR_MAP = {
    '001': (140.00, 'R$ 140,00'),  # ADVOGADO
    '002': (100.00, 'R$ 100,00'),  # AGENTE ADMINISTRATIVO
    '003': (100.00, 'R$ 100,00'),  # AUXILIAR ADMINISTRATIVO
    '004': (140.00, 'R$ 140,00'),  # BIOMÉDICO
    '005': (100.00, 'R$ 100,00'),  # ELETROTÉCNICO
    '006': (140.00, 'R$ 140,00'),  # ENFERMEIRO PLANTONISTA
    '007': (140.00, 'R$ 140,00'),  # ENFERMEIRO PSF
    '008': (100.00, 'R$ 100,00'),  # FISCAL DE SERVIÇOS PÚBLICOS
    '009': (140.00, 'R$ 140,00'),  # FISIOTERAPEUTA
    '010': (140.00, 'R$ 140,00'),  # FONOAUDIÓLOGO
    '011': (100.00, 'R$ 100,00'),  # MOTORISTA CAT.B
    '012': (100.00, 'R$ 100,00'),  # MOTORISTA CAT.C
    '013': (100.00, 'R$ 100,00'),  # MOTORISTA CAT.D
    '014': (140.00, 'R$ 140,00'),  # NUTRICIONISTA
    '015': (140.00, 'R$ 140,00'),  # PSICÓLOGO
    '016': (100.00, 'R$ 100,00'),  # TÉCNICO EM ENFERMAGEM
    '017': (100.00, 'R$ 100,00'),  # TÉCNICO EM INFORMÁTICA
    '018': (100.00, 'R$ 100,00'),  # TÉCNICO EM RADIOLOGIA
    '019': (140.00, 'R$ 140,00'),  # TERAPEUTA OCUPACIONAL
    '020': (140.00, 'R$ 140,00'),  # VETERINÁRIO
    '021': (100.00, 'R$ 100,00'),  # VIGILANTE
}


def _valor_por_cargo(cargo: str, fallback: float = 0.0) -> float:
    """Extrai o cargo_codigo (primeiros 3 dígitos) e retorna o valor conforme edital."""
    if not cargo:
        return fallback
    # Aceita "001", "001 - ADVOGADO", " 001-Advogado", etc.
    s = str(cargo).strip()
    codigo = ''
    for ch in s:
        if ch.isdigit():
            codigo += ch
            if len(codigo) == 3:
                break
        elif codigo:
            break
    v = CARGO_VALOR_MAP.get(codigo)
    return v[0] if v else fallback


def _cpf_digits(v: str) -> str:
    return ''.join(ch for ch in (v or '') if ch.isdigit())


async def _save_upload(u: UploadFile, prefix: str) -> dict:
    """Salva UploadFile no GridFS e retorna metadata no formato esperado pelo admin."""
    if not u or not u.filename:
        return {}
    ext = ''
    if '.' in u.filename:
        ext = '.' + u.filename.rsplit('.', 1)[-1].lower()
    fname = f"{prefix}_{secrets.token_hex(8)}{ext}"
    data = await u.read()
    ctype = u.content_type or 'application/octet-stream'
    await storage.save_bytes(fname, data, ctype)
    return {
        'filename': fname,
        'original_name': u.filename,
        'content_type': ctype,
        'size': len(data),
    }


@api_router.post('/inscricoes/submit')
async def inscricao_submit(
    payload: str = Form(...),
    doc_frente: Optional[UploadFile] = File(None),
    doc_verso: Optional[UploadFile] = File(None),
):
    """
    Endpoint público chamado pelo formulário de inscrição.
    Salva arquivos, cria/atualiza cadastro, registra inscrição e evento —
    tudo no mesmo schema esperado pelo painel admin.
    """
    try:
        data = json.loads(payload)
    except Exception:
        raise HTTPException(400, "payload inválido")

    if not isinstance(data, dict):
        raise HTTPException(400, "payload deve ser um objeto")

    cpf = _cpf_digits(data.get('cpf', ''))
    nome = (data.get('nome') or '').strip()
    if not cpf or not nome:
        raise HTTPException(400, "CPF e nome são obrigatórios")

    email = (data.get('email') or '').strip()
    concurso = data.get('concurso') or 'PREFEITURA MUNICIPAL DE MOREILÂNDIA-PE — EDITAL 001/2026'
    cargo = data.get('cargo') or ''
    tipo_documento = data.get('tipoDocumento') or ''
    protocolo = data.get('protocolo') or ''.join([str(secrets.randbelow(10)) for _ in range(10)])

    now = datetime.now(timezone.utc)

    frente_meta = await _save_upload(doc_frente, f"{cpf}_frente") if doc_frente else {}
    verso_meta = await _save_upload(doc_verso, f"{cpf}_verso") if doc_verso else {}

    # 1) Upsert no cadastro (formato esperado pelo painel /admin/documents)
    set_fields = {
        'nome': nome,
        'cpf': cpf,
        'email': email,
        'last_concurso': concurso,
        'last_at': now,
        'tipo_documento': tipo_documento,
        'form_data': data,
    }
    if frente_meta:
        set_fields['documento_frente'] = frente_meta
    if verso_meta:
        set_fields['documento_verso'] = verso_meta
    if frente_meta or verso_meta:
        set_fields['docs_updated_at'] = now

    await db.cadastros.update_one(
        {'cpf': cpf},
        {'$set': set_fields,
         '$setOnInsert': {'created_at': now, 'inscricoes_count': 0}},
        upsert=True,
    )

    # 2) Log em registrations
    await db.registrations.insert_one({
        'nome': nome, 'cpf': cpf, 'concurso': concurso,
        'stage': 'inscricao_finalizada', 'created_at': now,
    })

    # 3) Inscrição finalizada
    settings_doc = await db.settings.find_one({'_id': 'main'}, {'valor_inscricao': 1}) or {}
    try:
        valor_settings = float(settings_doc.get('valor_inscricao') or 0)
    except Exception:
        valor_settings = 0.0
    # Prioriza o valor da tabela do edital (cargo → taxa). Se cargo desconhecido, usa fallback do settings.
    valor_default = _valor_por_cargo(cargo, fallback=valor_settings)
    insc_id = str(uuid.uuid4())
    insc_doc = {
        'id': insc_id,
        'nome': nome, 'cpf': cpf, 'email': email,
        'concurso': concurso,
        'cargo_titulo': cargo,
        'cargo_codigo': (cargo.split(' - ')[0] if ' - ' in cargo else ''),
        'protocolo': protocolo,
        'valor': valor_default,
        'finalized': True,
        'finalized_at': now,
        'created_at': now,
        'pix_status': 'Aguardando pagamento',
        'pix_status_at': now,
    }
    await db.inscricoes.update_one(
        {'cpf': cpf, 'cargo_codigo': insc_doc['cargo_codigo']},
        {'$set': {k: v for k, v in insc_doc.items() if k not in ('id', 'created_at')},
         '$setOnInsert': {'id': insc_id, 'created_at': now}},
        upsert=True,
    )
    await db.cadastros.update_one({'cpf': cpf}, {'$inc': {'inscricoes_count': 1}})

    # 4) Evento no feed do painel
    await db.events.insert_one({
        'kind': 'registration',
        'description': f"Inscrição finalizada - {nome}",
        'meta': {'nome': nome, 'cpf': cpf, 'cargo': cargo, 'protocolo': protocolo, 'concurso': concurso},
        'created_at': now,
    })

    # 5) Notificação Telegram (envia mensagem inicial com status "Aguardando pagamento")
    try:
        from admin_routes import notify_or_update_telegram
        await notify_or_update_telegram(cpf, extra={
            'nome': nome, 'cpf': cpf, 'cargo': cargo, 'protocolo': protocolo,
            'concurso': concurso, 'valor': valor_default,
        })
    except Exception as e:
        import logging
        logging.warning(f"telegram notify falhou: {e}")

    return {'ok': True, 'protocolo': protocolo, 'cpf': cpf}


@api_router.get('/inscricao-valor')
async def get_valor_inscricao(cargo: str = ''):
    """Retorna a taxa de inscrição.

    - Se `cargo` for informado (código "001" ou "001 - ADVOGADO"), aplica o mapa do edital
      (R$ 140,00 nível superior / R$ 100,00 nível médio-técnico).
    - Se não informado, cai no `settings.valor_inscricao` como fallback (compat).
    """
    s = await db.settings.find_one({'_id': 'main'}, {'valor_inscricao': 1}) or {}
    try:
        fallback = float(s.get('valor_inscricao') or 0)
    except Exception:
        fallback = 0.0
    v = _valor_por_cargo(cargo, fallback=fallback) if cargo else fallback
    return {'valor': v, 'cargo': cargo or ''}


app.include_router(api_router)



@app.on_event("startup")
async def on_startup():
    await seed_admin()


app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
