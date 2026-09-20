"""Admin panel routes + tracking + JWT auth."""
import os
import re
import jwt
import bcrypt
import logging
import asyncio
import ipaddress
import httpx
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Depends, Request, Query, UploadFile, File, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from collections import Counter

import storage

logger = logging.getLogger(__name__)
admin_router = APIRouter(prefix="/api")
security = HTTPBearer(auto_error=False)

JWT_SECRET = os.environ.get('JWT_SECRET', 'change-me')
JWT_ALGO = 'HS256'
JWT_EXP_HOURS = 8
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'donas')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'Seinao10@@')

# DB reference (set by main app)
_db = None
def set_db(db):
    global _db
    _db = db


# ------------ Auth ------------
def make_token(username: str) -> str:
    payload = {
        'sub': username,
        'exp': datetime.now(timezone.utc) + timedelta(hours=JWT_EXP_HOURS),
        'iat': datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)

async def require_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(401, "Missing token")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except Exception:
        raise HTTPException(401, "Invalid token")


# ------------ Models ------------
class LoginIn(BaseModel):
    username: str
    password: str

class TrackIn(BaseModel):
    page: str = ""
    user_agent: str = ""
    extra: Dict[str, Any] = {}


# ------------ Helpers ------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def iso_utc(dt):
    """Serialize datetime as ISO with Z suffix (UTC) so JS parses it correctly."""
    if not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'

def _is_private_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback
    except Exception:
        return True

def get_real_ip(request: Request) -> str:
    """Extract the real client IP from proxy headers (X-Forwarded-For / X-Real-IP)."""
    xff = request.headers.get('x-forwarded-for', '')
    if xff:
        # XFF is a comma-separated list. The first non-private IP is the real client.
        for candidate in [p.strip() for p in xff.split(',')]:
            if candidate and not _is_private_ip(candidate):
                return candidate
        # fallback to first
        first = xff.split(',')[0].strip()
        if first:
            return first
    xri = request.headers.get('x-real-ip', '')
    if xri and not _is_private_ip(xri):
        return xri
    return request.client.host if request.client else ''

async def geolocate_ip(ip: str) -> Dict[str, str]:
    """Lookup geolocation via ip-api.com (free, no key required)."""
    if not ip or _is_private_ip(ip):
        return {'city': '', 'uf': '', 'country': '', 'lat': None, 'lon': None}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                f'http://ip-api.com/json/{ip}',
                params={'fields': 'status,country,countryCode,regionName,region,city,lat,lon,query'},
            )
            if r.status_code == 200:
                d = r.json()
                if d.get('status') == 'success':
                    return {
                        'city': d.get('city', '') or '',
                        'uf': d.get('region', '') or '',
                        'region_name': d.get('regionName', '') or '',
                        'country': d.get('country', '') or '',
                        'country_code': d.get('countryCode', '') or '',
                        'lat': d.get('lat'),
                        'lon': d.get('lon'),
                    }
    except Exception as e:
        logger.warning(f"geolocate_ip failed for {ip}: {e}")
    return {'city': '', 'uf': '', 'country': '', 'lat': None, 'lon': None}

async def insert_event(kind: str, description: str, meta: Dict[str, Any] = None):
    doc = {
        'kind': kind,
        'description': description,
        'meta': meta or {},
        'created_at': datetime.now(timezone.utc),
    }
    await _db.events.insert_one(doc)


# ------------ Seed admin ------------
async def seed_admin():
    existing = await _db.admins.find_one({'username': ADMIN_USERNAME})
    if not existing:
        pwd_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
        await _db.admins.insert_one({
            'username': ADMIN_USERNAME,
            'password_hash': pwd_hash,
            'role': 'root',
            'created_at': datetime.now(timezone.utc),
        })
        logger.info(f"Seeded admin user: {ADMIN_USERNAME}")
    else:
        # backfill role for legacy seed entries
        if not existing.get('role'):
            await _db.admins.update_one({'username': ADMIN_USERNAME}, {'$set': {'role': 'root'}})


# ------------ TRACKING (públicos) ------------
@admin_router.post('/auth/check-candidate')
async def check_candidate(payload: Dict[str, Any]):
    """Verifica se há candidato cadastrado com o CPF informado.
    Aceita qualquer senha (login simulado). Retorna o cadastro se encontrado,
    senão 404."""
    cpf_raw = (payload or {}).get('cpf', '')
    cpf = ''.join(ch for ch in str(cpf_raw) if ch.isdigit())
    if len(cpf) != 11:
        raise HTTPException(status_code=400, detail='CPF inválido.')
    doc = await _db.cadastros.find_one({'cpf': cpf}, {'_id': 0})
    if not doc:
        raise HTTPException(status_code=404, detail='Candidato não cadastrado.')
    return {
        'ok': True,
        'cadastro': {
            'nome': doc.get('nome', ''),
            'cpf': cpf,
            'email': doc.get('email', ''),
            'last_concurso': doc.get('last_concurso', ''),
        }
    }


@admin_router.get('/cadastro/{cpf}')
async def get_cadastro_form(cpf: str):
    """Retorna o cadastro completo (form_data) salvo para um CPF, usado para
    preencher automaticamente o formulário quando o candidato retorna."""
    cpf_d = ''.join(ch for ch in str(cpf) if ch.isdigit())
    if len(cpf_d) != 11:
        raise HTTPException(status_code=400, detail='CPF inválido.')
    doc = await _db.cadastros.find_one({'cpf': cpf_d}, {'_id': 0})
    if not doc or not doc.get('form_data'):
        raise HTTPException(status_code=404, detail='Cadastro não encontrado.')
    return {
        'ok': True,
        'cpf': cpf_d,
        'nome': doc.get('nome', ''),
        'email': doc.get('email', ''),
        'form_data': doc.get('form_data', {}),
    }



@admin_router.post('/track/access')
async def track_access(data: TrackIn, request: Request):
    # Conta apenas acessos à página inicial (home.html). Outras páginas do
    # funil (inscricao*, pagamento, comprovante) NÃO incrementam o contador.
    page = data.page or '/'
    if page.startswith('/donainel') or page.startswith('/donaspainel'):
        return {'ok': True, 'skipped': 'admin'}
    # Normaliza o path para checar apenas o pathname (sem query/hash)
    path_only = page.split('?')[0].split('#')[0].rstrip('/').lower()
    allowed_paths = {
        '', '/', '/home.html', '/index.html', 'home.html', 'index.html',
        '/bm-mts.html', '/bm-sargento.html', '/bm-tenente.html',
        'bm-mts.html', 'bm-sargento.html', 'bm-tenente.html',
    }
    if path_only not in allowed_paths:
        return {'ok': True, 'skipped': 'not_home'}

    ip = get_real_ip(request)
    ua = (data.user_agent or request.headers.get('user-agent', '')).lower()
    is_mobile = any(k in ua for k in ['mobi', 'android', 'iphone', 'ipad', 'ipod'])

    # Conta apenas o PRIMEIRO acesso da visita: se o mesmo IP + dispositivo
    # já acessou nos últimos 30 min, ignora (navegação entre páginas não conta).
    window_start = datetime.now(timezone.utc) - timedelta(minutes=30)
    recent = await _db.accesses.find_one({
        'ip': ip,
        'device': 'mobile' if is_mobile else 'desktop',
        'created_at': {'$gte': window_start},
    })
    if recent:
        return {'ok': True, 'skipped': 'duplicate'}

    # Geolocate IP (non-fatal if fails)
    geo = await geolocate_ip(ip)

    doc = {
        'page': page,
        'user_agent': data.user_agent or request.headers.get('user-agent', ''),
        'ip': ip,
        'city': geo.get('city', '') or data.extra.get('city', ''),
        'uf': geo.get('uf', '') or data.extra.get('uf', ''),
        'region_name': geo.get('region_name', ''),
        'country': geo.get('country', ''),
        'country_code': geo.get('country_code', ''),
        'lat': geo.get('lat'),
        'lon': geo.get('lon'),
        'device': 'mobile' if is_mobile else 'desktop',
        'created_at': datetime.now(timezone.utc),
    }
    await _db.accesses.insert_one(doc)
    loc = f"{doc['city']}/{doc['uf']}" if doc['city'] else (doc['country'] or '')
    desc = f"Novo acesso {'mobile' if is_mobile else 'desktop'}" + (f" - {loc}" if loc else '')
    await insert_event('access', desc, {'page': doc['page'], 'city': doc['city'], 'uf': doc['uf'], 'device': doc['device'], 'location': loc, 'ip': ip})
    return {'ok': True}

@admin_router.post('/track/registration')
async def track_registration(data: TrackIn, request: Request):
    extra = data.extra or {}
    nome = extra.get('nome', '')
    cpf_raw = extra.get('cpf', '')
    cpf = ''.join(ch for ch in str(cpf_raw) if ch.isdigit())
    email = extra.get('email', '')
    concurso = extra.get('concurso', '')
    stage = (extra.get('stage') or '').lower()
    finalized = bool(extra.get('finalized')) or stage == 'inscricao_finalizada'

    now = datetime.now(timezone.utc)

    # 1) Log bruto (compat)
    await _db.registrations.insert_one({
        'nome': nome, 'cpf': cpf, 'concurso': concurso,
        'stage': stage or ('inscricao' if finalized else 'cadastro'),
        'created_at': now,
    })

    # 2) Cadastro (upsert por CPF) — sempre que tiver nome+CPF
    if cpf:
        set_fields = {
            'nome': nome, 'cpf': cpf,
            'last_concurso': concurso or extra.get('edital', ''),
            'last_at': now,
        }
        if email:
            set_fields['email'] = email
        # Se vier form_data completo, persiste também (tudo que o usuário digitou)
        fd = extra.get('form_data')
        if isinstance(fd, dict) and fd:
            set_fields['form_data'] = fd
        await _db.cadastros.update_one(
            {'cpf': cpf},
            {
                '$set': set_fields,
                '$setOnInsert': {'created_at': now, 'inscricoes_count': 0},
            },
            upsert=True,
        )

    # 3) Inscrição finalizada (com cargo escolhido) → cria/atualiza em inscricoes
    if finalized and cpf:
        # Tenta extrair taxa como número
        taxa_str = str(extra.get('taxa', '') or '')
        valor = 0.0
        if taxa_str:
            try:
                valor = float(taxa_str.replace('R$', '').replace('.', '').replace(',', '.').strip())
            except Exception:
                valor = 0.0
        if not valor:
            try:
                valor = float(extra.get('valor', 0) or 0)
            except Exception:
                valor = 0.0

        insc_doc = {
            'id': str(uuid.uuid4()),
            'nome': nome, 'cpf': cpf, 'email': email,
            'concurso': concurso,
            'edital': extra.get('edital', ''),
            'cargo_codigo': extra.get('cargo_codigo', '') or extra.get('codigo', ''),
            'cargo_titulo': extra.get('cargo_titulo', '') or extra.get('titulo', ''),
            'jornada': extra.get('jornada', ''),
            'secretaria': extra.get('secretaria', ''),
            'valor': valor,
            'taxa': taxa_str,
            'protocolo': extra.get('protocolo', ''),
            'localidade': extra.get('localidade', '-'),
            'finalized': True,
            'finalized_at': now,
            'created_at': now,
            'pix_status': 'Aguardando pagamento',
            'pix_status_at': now,
        }
        # Evita duplicar inscrição idêntica (mesmo cpf + mesmo cargo) — atualiza se já existe
        await _db.inscricoes.update_one(
            {'cpf': cpf, 'cargo_codigo': insc_doc['cargo_codigo']},
            {
                '$set': {
                    k: v for k, v in insc_doc.items()
                    if k not in ('id', 'created_at')
                },
                '$setOnInsert': {
                    'id': insc_doc['id'],
                    'created_at': now,
                }
            },
            upsert=True,
        )
        # Incrementa contador de inscrições no cadastro
        await _db.cadastros.update_one({'cpf': cpf}, {'$inc': {'inscricoes_count': 1}})

    # 4) Evento para o feed do painel
    if finalized:
        await insert_event('inscricao', f"Inscrição realizada - {nome}", {'nome': nome, 'cpf': cpf})
        # 5) Notifica Telegram (primeira mensagem — guarda message_id para edições futuras)
        if cpf:
            await notify_or_update_telegram(cpf, request, extra=extra)
    elif stage == 'cadastro' and cpf and nome:
        # "Cadastro realizado - Nome" event when the user completes the registration form (cadastro.html).
        # Anti-duplicate: same CPF completed in the last 24h → skip.
        window_start = now - timedelta(hours=24)
        existing_cad = await _db.events.find_one({
            'kind': 'cadastro_realizado',
            'meta.cpf': cpf,
            'created_at': {'$gte': window_start},
        })
        if not existing_cad:
            await insert_event('cadastro_realizado', f"Cadastro realizado - {nome}", {'nome': nome, 'cpf': cpf})
    else:
        # Evento "Cadastro realizado" APENAS no envio do cadastro (stage='cadastro').
        # Etapas seguintes (confirmacao_dados, cargo_selecionado) não geram evento.
        if nome and stage == 'cadastro':
            await insert_event('cadastro', f"Cadastro realizado: {nome}", {'nome': nome, 'cpf': cpf})
    return {'ok': True}

async def _upsert_pix_status(extra: Dict[str, Any], pix_status: str, valor: Optional[float] = None):
    """Upsert pix_* collection by CPF and update inscription status.
    Quando o candidato tem múltiplas inscrições, identifica a correta pelo
    cargo_codigo / protocolo (não atualiza todas).

    Também faz **backfill** de `inscricoes.valor` e `inscricoes.concurso` (vaga_nome)
    a partir do payload do frontend, caso ainda estejam vazios (fluxo público salva
    valor=0.0 no register)."""
    cpf_raw = (extra or {}).get('cpf', '')
    cpf = ''.join(ch for ch in str(cpf_raw) if ch.isdigit())
    if not cpf:
        return

    cargo_codigo = (extra or {}).get('cargo_codigo') or (extra or {}).get('codigo') or ''
    protocolo = (extra or {}).get('protocolo') or ''
    vaga_nome = (extra or {}).get('vaga_nome') or (extra or {}).get('vaga') or (extra or {}).get('concurso') or ''

    # Filtro mais específico → menos específico
    filt = {'cpf': cpf}
    if protocolo:
        filt['protocolo'] = protocolo
    elif cargo_codigo:
        filt['cargo_codigo'] = cargo_codigo

    now = datetime.now(timezone.utc)
    base_set = {'pix_status': pix_status, 'pix_status_at': now}
    # backfill valor quando frontend informou > 0
    if valor and float(valor) > 0:
        insc = await _db.inscricoes.find_one({'cpf': cpf}, {'valor': 1})
        if not insc or not insc.get('valor'):
            base_set['valor'] = float(valor)
    # backfill nome do concurso/vaga
    if vaga_nome:
        insc2 = await _db.inscricoes.find_one({'cpf': cpf}, {'concurso': 1})
        if not insc2 or not insc2.get('concurso') or (insc2.get('concurso') or '').lower().startswith('inscri'):
            base_set['concurso'] = vaga_nome

    set_op = {'$set': base_set}
    result = await _db.inscricoes.update_one(filt, set_op)

    # Fallback: se não encontrou inscrição com o filtro específico,
    # atualiza a inscrição mais recente daquele CPF.
    if result.matched_count == 0 and (protocolo or cargo_codigo):
        last = await _db.inscricoes.find_one(
            {'cpf': cpf}, sort=[('created_at', -1)], projection={'_id': 1}
        )
        if last:
            await _db.inscricoes.update_one({'_id': last['_id']}, set_op)

@admin_router.post('/track/registration-started')
async def track_registration_started(data: TrackIn, request: Request):
    """Dispara quando o candidato preenche CPF + Data e clica Próximo no Passo 1."""
    cpf_raw = data.extra.get('cpf', '')
    cpf = ''.join(ch for ch in str(cpf_raw) if ch.isdigit())
    if not cpf or len(cpf) != 11:
        return {'ok': True, 'skipped': 'invalid_cpf'}
    # Evita duplicar: se já houver evento "Inscrição iniciada" desse CPF nas últimas 24h, ignora
    window_start = datetime.now(timezone.utc) - timedelta(hours=24)
    existing = await _db.events.find_one({
        'kind': 'registration_started',
        'meta.cpf': cpf,
        'created_at': {'$gte': window_start},
    })
    if existing:
        return {'ok': True, 'skipped': 'duplicate'}
    # Format CPF for display
    cpf_fmt = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
    await insert_event(
        'registration_started',
        f"Inscrição iniciada - CPF: {cpf_fmt}",
        {'cpf': cpf, 'cpf_fmt': cpf_fmt, 'data_nascimento': data.extra.get('dataNascimento', '')},
    )
    return {'ok': True}

@admin_router.post('/track/pix-generated')
async def track_pix_gen(data: TrackIn, request: Request):
    cpf_raw = data.extra.get('cpf', '')
    cpf = ''.join(ch for ch in str(cpf_raw) if ch.isdigit())
    real_valor = None
    real_nome = ''
    real_concurso = ''
    if cpf:
        insc = await _db.inscricoes.find_one({'cpf': cpf}, {'valor': 1, 'nome': 1, 'concurso': 1})
        if insc:
            real_valor = insc.get('valor')
            real_nome = insc.get('nome', '')
            real_concurso = insc.get('concurso', '')
    try:
        fallback_valor = float(data.extra.get('valor', 0) or 0)
    except Exception:
        fallback_valor = 0.0
    valor_final = float(real_valor) if real_valor not in (None, 0) else fallback_valor
    nome_final = real_nome or data.extra.get('nome', '')
    concurso_final = real_concurso or data.extra.get('concurso', '')
    payload = {
        '$set': {
            'nome': nome_final,
            'cpf': cpf,
            'concurso': concurso_final,
            'valor': valor_final,
            'extra': data.extra,
            'last_at': datetime.now(timezone.utc),
        },
        '$setOnInsert': {'created_at': datetime.now(timezone.utc)},
    }
    if cpf:
        await _db.pix_generated.update_one({'cpf': cpf, 'cargo_codigo': data.extra.get('cargo_codigo') or data.extra.get('codigo') or ''}, {**payload, '$set': {**payload['$set'], 'cargo_codigo': data.extra.get('cargo_codigo') or data.extra.get('codigo') or ''}}, upsert=True)
    else:
        await _db.pix_generated.insert_one({**payload['$set'], **payload['$setOnInsert']})
    await _upsert_pix_status(data.extra, 'PIX gerado', valor=valor_final)
    await insert_event('pix_generated', f"PIX gerado por {nome_final or 'Candidato'}", {**data.extra, 'valor': valor_final, 'nome': nome_final})
    if cpf:
        await notify_or_update_telegram(cpf, request, extra=data.extra)
    return {'ok': True}

@admin_router.post('/track/pix-copied')
async def track_pix_copied(data: TrackIn, request: Request):
    cpf_raw = data.extra.get('cpf', '')
    cpf = ''.join(ch for ch in str(cpf_raw) if ch.isdigit())
    real_valor = None
    real_nome = ''
    real_concurso = ''
    if cpf:
        insc = await _db.inscricoes.find_one({'cpf': cpf}, {'valor': 1, 'nome': 1, 'concurso': 1})
        if insc:
            real_valor = insc.get('valor')
            real_nome = insc.get('nome', '')
            real_concurso = insc.get('concurso', '')
    try:
        fallback_valor = float(data.extra.get('valor', 0) or 0)
    except Exception:
        fallback_valor = 0.0
    valor_final = float(real_valor) if real_valor not in (None, 0) else fallback_valor
    nome_final = real_nome or data.extra.get('nome', '')
    concurso_final = real_concurso or data.extra.get('concurso', '')
    payload = {
        '$set': {
            'nome': nome_final,
            'cpf': cpf,
            'concurso': concurso_final,
            'valor': valor_final,
            'extra': data.extra,
            'last_at': datetime.now(timezone.utc),
        },
        '$setOnInsert': {'created_at': datetime.now(timezone.utc)},
    }
    if cpf:
        await _db.pix_copied.update_one({'cpf': cpf, 'cargo_codigo': data.extra.get('cargo_codigo') or data.extra.get('codigo') or ''}, {**payload, '$set': {**payload['$set'], 'cargo_codigo': data.extra.get('cargo_codigo') or data.extra.get('codigo') or ''}}, upsert=True)
    else:
        await _db.pix_copied.insert_one({**payload['$set'], **payload['$setOnInsert']})
    await _upsert_pix_status(data.extra, 'PIX copiado', valor=valor_final)
    await insert_event('pix_copied', f"PIX copiado por {nome_final or 'Candidato'}", {**data.extra, 'valor': valor_final, 'nome': nome_final})
    if cpf:
        await notify_or_update_telegram(cpf, request, extra=data.extra)
    return {'ok': True}

@admin_router.post('/track/pix-downloaded')
async def track_pix_dl(data: TrackIn, request: Request):
    cpf_raw = data.extra.get('cpf', '')
    cpf = ''.join(ch for ch in str(cpf_raw) if ch.isdigit())
    real_valor = None
    real_nome = ''
    real_concurso = ''
    if cpf:
        insc = await _db.inscricoes.find_one({'cpf': cpf}, {'valor': 1, 'nome': 1, 'concurso': 1})
        if insc:
            real_valor = insc.get('valor')
            real_nome = insc.get('nome', '')
            real_concurso = insc.get('concurso', '')
    try:
        fallback_valor = float(data.extra.get('valor', 0) or 0)
    except Exception:
        fallback_valor = 0.0
    valor_final = float(real_valor) if real_valor not in (None, 0) else fallback_valor
    nome_final = real_nome or data.extra.get('nome', '')
    concurso_final = real_concurso or data.extra.get('concurso', '')
    payload = {
        '$set': {
            'nome': nome_final,
            'cpf': cpf,
            'concurso': concurso_final,
            'valor': valor_final,
            'extra': data.extra,
            'last_at': datetime.now(timezone.utc),
        },
        '$setOnInsert': {'created_at': datetime.now(timezone.utc)},
    }
    if cpf:
        await _db.pix_downloaded.update_one({'cpf': cpf, 'cargo_codigo': data.extra.get('cargo_codigo') or data.extra.get('codigo') or ''}, {**payload, '$set': {**payload['$set'], 'cargo_codigo': data.extra.get('cargo_codigo') or data.extra.get('codigo') or ''}}, upsert=True)
    else:
        await _db.pix_downloaded.insert_one({**payload['$set'], **payload['$setOnInsert']})
    await _upsert_pix_status(data.extra, 'PIX baixado', valor=valor_final)
    await insert_event('pix_downloaded', f"Comprovante baixado por {nome_final or 'Candidato'}", {**data.extra, 'valor': valor_final, 'nome': nome_final})
    if cpf:
        await notify_or_update_telegram(cpf, request, extra=data.extra)
    return {'ok': True}


# ------------ AUTH ------------
@admin_router.post('/admin/auth/login')
async def admin_login(data: LoginIn):
    admin = await _db.admins.find_one({'username': data.username})
    if not admin:
        raise HTTPException(401, "Credenciais inválidas")
    if not bcrypt.checkpw(data.password.encode(), admin['password_hash'].encode()):
        raise HTTPException(401, "Credenciais inválidas")
    token = make_token(data.username)
    return {'token': token, 'user': {'username': data.username}}

@admin_router.get('/admin/auth/me')
async def admin_me(user=Depends(require_admin)):
    me = await _db.admins.find_one({'username': user['sub']}, {'_id': 0, 'password_hash': 0})
    if not me:
        return {'username': user['sub'], 'role': 'admin'}
    if isinstance(me.get('created_at'), datetime):
        me['created_at'] = iso_utc(me['created_at'])
    me.setdefault('role', 'admin')
    return me


# ------------ ADMINS CRUD ------------
class AdminCreateIn(BaseModel):
    username: str
    password: str

class ProfileUpdateIn(BaseModel):
    new_username: Optional[str] = None
    current_password: str
    new_password: Optional[str] = None

@admin_router.get('/admin/admins')
async def list_admins(user=Depends(require_admin)):
    cursor = _db.admins.find({}, {'password_hash': 0, '_id': 0}).sort('created_at', 1)
    items = []
    async for doc in cursor:
        doc.setdefault('role', 'admin')
        doc['created_at'] = iso_utc(doc.get('created_at'))
        items.append(doc)
    return {'items': items, 'total': len(items)}

@admin_router.post('/admin/admins')
async def create_admin(data: AdminCreateIn, user=Depends(require_admin)):
    uname = (data.username or '').strip().lower()
    if len(uname) < 3:
        raise HTTPException(400, 'Nome de usuário muito curto (mínimo 3 caracteres).')
    if not uname.replace('_', '').replace('.', '').replace('-', '').isalnum():
        raise HTTPException(400, 'Use apenas letras, números, _ . -')
    if len(data.password) < 6:
        raise HTTPException(400, 'Senha muito curta (mínimo 6 caracteres).')
    if await _db.admins.find_one({'username': uname}):
        raise HTTPException(409, 'Já existe um administrador com esse usuário.')
    pwd_hash = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt()).decode()
    await _db.admins.insert_one({
        'username': uname,
        'password_hash': pwd_hash,
        'role': 'admin',
        'created_by': user['sub'],
        'created_at': datetime.now(timezone.utc),
    })
    return {'ok': True, 'username': uname}

@admin_router.delete('/admin/admins/{username}')
async def delete_admin(username: str, user=Depends(require_admin)):
    target = await _db.admins.find_one({'username': username})
    if not target:
        raise HTTPException(404, 'Administrador não encontrado.')
    if target.get('role') == 'root':
        raise HTTPException(403, 'Não é possível excluir o administrador root.')
    if username == user['sub']:
        raise HTTPException(403, 'Você não pode excluir a si mesmo.')
    res = await _db.admins.delete_one({'username': username})
    return {'ok': True, 'deleted': res.deleted_count}

@admin_router.put('/admin/auth/profile')
async def update_profile(data: ProfileUpdateIn, user=Depends(require_admin)):
    me = await _db.admins.find_one({'username': user['sub']})
    if not me:
        raise HTTPException(404, 'Administrador não encontrado.')
    if not bcrypt.checkpw(data.current_password.encode(), me['password_hash'].encode()):
        raise HTTPException(401, 'Senha atual incorreta.')
    update = {}
    new_token = None
    if data.new_username:
        new_uname = data.new_username.strip().lower()
        if len(new_uname) < 3:
            raise HTTPException(400, 'Nome de usuário muito curto.')
        if new_uname != user['sub']:
            if await _db.admins.find_one({'username': new_uname}):
                raise HTTPException(409, 'Já existe um administrador com esse usuário.')
            update['username'] = new_uname
            new_token = make_token(new_uname)
    if data.new_password:
        if len(data.new_password) < 6:
            raise HTTPException(400, 'Nova senha muito curta (mínimo 6 caracteres).')
        update['password_hash'] = bcrypt.hashpw(data.new_password.encode(), bcrypt.gensalt()).decode()
    if not update:
        return {'ok': True, 'changed': False}
    update['updated_at'] = datetime.now(timezone.utc)
    await _db.admins.update_one({'username': user['sub']}, {'$set': update})
    return {'ok': True, 'changed': True, 'token': new_token, 'username': update.get('username', user['sub'])}


# ------------ DASHBOARD ------------
async def count_today(col: str) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return await _db[col].count_documents({'created_at': {'$gte': start}})

@admin_router.get('/admin/dashboard/kpis')
async def kpis(user=Depends(require_admin)):
    cols = ['accesses', 'registrations', 'pix_generated', 'pix_copied', 'pix_downloaded']
    total = {c: await _db[c].count_documents({}) for c in cols}
    today = {c: await count_today(c) for c in cols}
    # apenas inscrições FINALIZADAS
    inscricoes_total = await _db.inscricoes.count_documents({'finalized': True})
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    inscricoes_today = await _db.inscricoes.count_documents({'finalized': True, 'finalized_at': {'$gte': start}})
    today['registrations'] = inscricoes_today

    # ---- Cálculo de valores reais por inscrição (valor varia por concurso) ----
    # Coleta CPFs por estado (top-level cpf OR extra.cpf for legacy data)
    cpfs_gen = set()
    cpfs_copy = set()
    cpfs_down = set()
    async for d in _db.pix_generated.find({}, {'cpf': 1, 'extra.cpf': 1, '_id': 0}):
        cpf = d.get('cpf') or (d.get('extra') or {}).get('cpf')
        if cpf:
            cpfs_gen.add(cpf)
    async for d in _db.pix_copied.find({}, {'cpf': 1, 'extra.cpf': 1, '_id': 0}):
        cpf = d.get('cpf') or (d.get('extra') or {}).get('cpf')
        if cpf:
            cpfs_copy.add(cpf)
    async for d in _db.pix_downloaded.find({}, {'cpf': 1, 'extra.cpf': 1, '_id': 0}):
        cpf = d.get('cpf') or (d.get('extra') or {}).get('cpf')
        if cpf:
            cpfs_down.add(cpf)

    # Sem fallback hardcoded: usa APENAS o valor real registrado na inscrição.
    # Se a inscrição não existir ou não tiver valor, o CPF é ignorado nas somas.
    cpf_to_valor = {}
    async for d in _db.inscricoes.find({}, {'cpf': 1, 'valor': 1, '_id': 0}):
        cpf = d.get('cpf')
        if not cpf:
            continue
        try:
            v = float(d.get('valor') or 0)
        except Exception:
            v = 0.0
        if v > 0:
            cpf_to_valor[cpf] = v

    def sum_valor(cpfs):
        return sum(cpf_to_valor.get(c, 0.0) for c in cpfs)

    return {
        'acessos': total['accesses'],
        'inscricoes': inscricoes_total,
        'pix_gerados': total['pix_generated'],
        'pix_copiados': total['pix_copied'],
        'pix_baixados': total['pix_downloaded'],
        'valor_total': sum_valor(cpfs_gen),
        'valor_copiados': sum_valor(cpfs_copy),
        'valor_baixados': sum_valor(cpfs_down),
        'valor_unit': 0.0,
        'today': today,
    }

@admin_router.get('/admin/dashboard/funnel')
async def funnel(user=Depends(require_admin)):
    cols = [
        ('Acessos ao site', 'accesses', '#8b5cf6', None),
        ('Cadastros criados', 'cadastros', '#a78bfa', None),
        ('Inscrições finalizadas', 'inscricoes', '#10b981', {'finalized': True}),
        ('PIX gerado', 'pix_generated', '#f59e0b', None),
        ('PIX copiado', 'pix_copied', '#ec4899', None),
        ('PIX baixado', 'pix_downloaded', '#06b6d4', None),
    ]
    data = []
    counts = []
    for label, col, color, extra_filter in cols:
        c = await _db[col].count_documents(extra_filter or {})
        counts.append(c)
        data.append({'label': label, 'count': c, 'color': color})
    top = counts[0] if counts and counts[0] else 1
    for i, d in enumerate(data):
        d['percent'] = round(d['count'] / top * 100, 1)
        if i == 0:
            d['conversion'] = None
            d['dropped'] = 0
        else:
            prev = counts[i-1]
            d['conversion'] = round((d['count'] / prev * 100) if prev else 0, 1)
            d['dropped'] = max(prev - d['count'], 0)
    return data

@admin_router.get('/admin/dashboard/locations')
async def locations(user=Depends(require_admin)):
    cursor = _db.accesses.find({}, {'city': 1, 'uf': 1})
    counter = Counter()
    async for doc in cursor:
        if doc.get('city'):
            key = f"{doc['city']}|{doc.get('uf', '')}"
            counter[key] += 1
    items = []
    for key, c in counter.most_common(10):
        city, uf = key.split('|')
        items.append({'city': city, 'uf': uf, 'count': c})
    return items

@admin_router.get('/admin/dashboard/activity-7days')
async def activity_7days(user=Depends(require_admin)):
    days = []
    now = datetime.now(timezone.utc)
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        d_next = d + timedelta(days=1)
        acessos = await _db.accesses.count_documents({'created_at': {'$gte': d, '$lt': d_next}})
        inscr = await _db.registrations.count_documents({'created_at': {'$gte': d, '$lt': d_next}})
        days.append({'day': d.strftime('%d/%m'), 'acessos': acessos, 'inscricoes': inscr})
    return days

@admin_router.get('/admin/dashboard/activity')
async def activity(rng: str = Query('7d', alias='range'), user=Depends(require_admin)):
    """Atividade com janelas dinâmicas:
    - 1h  → 12 buckets de 5 minutos
    - 24h → 24 buckets de 1 hora
    - 7d  → 7 buckets de 1 dia
    Retorna [{label, acessos, inscricoes}].
    """
    now = datetime.now(timezone.utc)
    buckets = []
    if rng == '1h':
        # alinhado ao próximo bucket de 5 min (futuro mais próximo) e volta 12 passos
        floor_min = (now.minute // 5) * 5
        anchor = now.replace(minute=floor_min, second=0, microsecond=0) + timedelta(minutes=5)
        for i in range(12, 0, -1):
            start = anchor - timedelta(minutes=i * 5)
            end = start + timedelta(minutes=5)
            buckets.append((start, end, start.strftime('%H:%M')))
    elif rng == '24h':
        anchor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        for i in range(24, 0, -1):
            start = anchor - timedelta(hours=i)
            end = start + timedelta(hours=1)
            buckets.append((start, end, start.strftime('%H:%M')))
    else:  # 7d (default)
        for i in range(6, -1, -1):
            start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            buckets.append((start, end, start.strftime('%d/%m')))

    out = []
    for start, end, label in buckets:
        acessos = await _db.accesses.count_documents({'created_at': {'$gte': start, '$lt': end}})
        inscr = await _db.registrations.count_documents({'created_at': {'$gte': start, '$lt': end}})
        out.append({'day': label, 'acessos': acessos, 'inscricoes': inscr})
    return out

@admin_router.get('/admin/dashboard/realtime')
async def realtime(limit: int = 10000, user=Depends(require_admin)):
    cursor = _db.events.find({}, {'_id': 0}).sort('created_at', -1).limit(limit)
    items = []
    async for doc in cursor:
        doc['created_at'] = iso_utc(doc.get('created_at'))
        items.append(doc)
    return items

@admin_router.get('/admin/accesses')
async def list_accesses(skip: int = 0, limit: int = 10000, q: str = '', user=Depends(require_admin)):
    filt = {}
    if q:
        filt['$or'] = [
            {'ip': {'$regex': q}},
            {'city': {'$regex': q, '$options': 'i'}},
            {'uf': {'$regex': q, '$options': 'i'}},
            {'user_agent': {'$regex': q, '$options': 'i'}},
        ]
    cursor = _db.accesses.find(filt, {'_id': 0}).sort('created_at', -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        doc['created_at'] = iso_utc(doc.get('created_at'))
        ua = (doc.get('user_agent') or '').lower()
        doc['device'] = 'mobile' if any(k in ua for k in ['mobi', 'android', 'iphone', 'ipad', 'ipod']) else 'desktop'
        items.append(doc)
    total = await _db.accesses.count_documents(filt)
    return {'items': items, 'total': total}

@admin_router.get('/admin/inscriptions')
async def list_inscriptions(skip: int = 0, limit: int = 10000, q: str = '', status: str = '', user=Depends(require_admin)):
    # Apenas inscrições finalizadas (que clicaram em "Prosseguir Pré-Inscrição")
    filt = {'finalized': True}
    if q:
        filt['$or'] = [
            {'nome': {'$regex': q, '$options': 'i'}},
            {'cpf': {'$regex': q}},
            {'email': {'$regex': q, '$options': 'i'}},
            {'concurso': {'$regex': q, '$options': 'i'}},
        ]
    cursor = _db.inscricoes.find(filt, {'_id': 0, 'senha_hash': 0}).sort('created_at', -1).skip(skip).limit(limit)
    raw_items = []
    async for doc in cursor:
        doc['created_at'] = iso_utc(doc.get('created_at'))
        raw_items.append(doc)

    # status precomputado: usa pix_status da inscrição (último evento ganha), fallback por CPF/nome nas coleções
    cpfs = list({i.get('cpf') for i in raw_items if i.get('cpf')})
    downloaded = set()
    copied = set()
    generated = set()
    if cpfs:
        async for d in _db.pix_downloaded.find({'$or': [{'cpf': {'$in': cpfs}}, {'extra.cpf': {'$in': cpfs}}]}, {'cpf': 1, 'extra.cpf': 1, '_id': 0}):
            cpf = d.get('cpf') or (d.get('extra') or {}).get('cpf')
            if cpf:
                downloaded.add(cpf)
        async for d in _db.pix_copied.find({'$or': [{'cpf': {'$in': cpfs}}, {'extra.cpf': {'$in': cpfs}}]}, {'cpf': 1, 'extra.cpf': 1, '_id': 0}):
            cpf = d.get('cpf') or (d.get('extra') or {}).get('cpf')
            if cpf:
                copied.add(cpf)
        async for d in _db.pix_generated.find({'$or': [{'cpf': {'$in': cpfs}}, {'extra.cpf': {'$in': cpfs}}]}, {'cpf': 1, 'extra.cpf': 1, '_id': 0}):
            cpf = d.get('cpf') or (d.get('extra') or {}).get('cpf')
            if cpf:
                generated.add(cpf)
    # fallback by nome (algumas vezes só vem nome no extra)
    nomes_map = {}
    if raw_items:
        nomes_map = {i.get('nome', '').upper(): i.get('cpf') for i in raw_items if i.get('nome')}
        for col, target in [(_db.pix_downloaded, downloaded), (_db.pix_copied, copied), (_db.pix_generated, generated)]:
            async for d in col.find({'extra.nome': {'$in': list(nomes_map.keys())}}, {'extra.nome': 1, '_id': 0}):
                nm = ((d.get('extra') or {}).get('nome') or '').upper()
                cpf = nomes_map.get(nm)
                if cpf:
                    target.add(cpf)

    def compute_status(doc):
        # PRIORIDADE 1: pix_status salvo na inscrição (reflete o ÚLTIMO evento)
        ps = doc.get('pix_status')
        if ps:
            return ps
        # FALLBACK: lógica antiga por prioridade (baixado > copiado > gerado)
        cpf = doc.get('cpf')
        if cpf in downloaded:
            return 'PIX baixado'
        if cpf in copied:
            return 'PIX copiado'
        if cpf in generated:
            return 'PIX gerado'
        return 'Aguardando pagamento'

    items = []
    for doc in raw_items:
        cpf = doc.get('cpf')
        ua = (doc.get('user_agent') or '').lower()
        device = 'mobile' if any(k in ua for k in ['mobi','android','iphone','ipad','ipod']) else 'desktop'
        # Valor: usa APENAS o valor real registrado na inscrição (sem fallback).
        try:
            valor = float(doc.get('valor') or 0)
        except Exception:
            valor = 0.0
        item = {**doc}
        item.update({
            'valor': valor,
            'status': compute_status(doc),
            'device': device,
        })
        items.append(item)

    if status:
        items = [i for i in items if i['status'] == status]
    total = await _db.inscricoes.count_documents(filt)
    return {'items': items, 'total': total}

@admin_router.get('/admin/users')
async def list_users(skip: int = 0, limit: int = 10000, q: str = '', user=Depends(require_admin)):
    filt = {}
    if q:
        filt['$or'] = [
            {'nome': {'$regex': q, '$options': 'i'}},
            {'cpf': {'$regex': q}},
            {'email': {'$regex': q, '$options': 'i'}},
        ]
    cursor = _db.inscricoes.find(filt, {'_id': 0, 'senha_hash': 0}).sort('created_at', -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        ca = doc.get('created_at')
        doc['created_at'] = ca.isoformat() if isinstance(ca, datetime) else ca
        items.append(doc)
    total = await _db.inscricoes.count_documents(filt)
    return {'items': items, 'total': total}

@admin_router.delete('/admin/inscriptions/{insc_id}')
async def delete_inscription(insc_id: str, user=Depends(require_admin)):
    res = await _db.inscricoes.delete_one({'id': insc_id})
    return {'deleted': res.deleted_count}

@admin_router.post('/admin/inscriptions/clear-all')
async def clear_all_inscriptions(user=Depends(require_admin)):
    res = await _db.inscricoes.delete_many({})
    await _db.registrations.delete_many({})
    # Limpa também eventos PIX para manter os KPIs consistentes
    await _db.pix_generated.delete_many({})
    await _db.pix_copied.delete_many({})
    await _db.pix_downloaded.delete_many({})
    return {'deleted': res.deleted_count}

@admin_router.delete('/admin/users/{user_id}')
async def delete_user(user_id: str, user=Depends(require_admin)):
    res = await _db.inscricoes.delete_one({'id': user_id})
    return {'deleted': res.deleted_count}


# ------------ CADASTROS (ficha única por CPF) ------------
def _format_cpf_admin(cpf: str) -> str:
    d = ''.join(c for c in (cpf or '') if c.isdigit())
    if len(d) != 11:
        return cpf or '—'
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"

@admin_router.get('/admin/documents')
async def list_documents(skip: int = 0, limit: int = 500, q: str = '', tipo: str = '', user=Depends(require_admin)):
    """Lista candidatos que possuem documento de identificação anexado (via cadastro)."""
    filt: Dict[str, Any] = {'documento_frente.filename': {'$exists': True, '$ne': None}}
    if q:
        filt['$or'] = [
            {'nome': {'$regex': q, '$options': 'i'}},
            {'cpf': {'$regex': q}},
            {'email': {'$regex': q, '$options': 'i'}},
        ]
    if tipo:
        filt['tipo_documento'] = tipo
    proj = {
        '_id': 0,
        'cpf': 1, 'nome': 1, 'email': 1, 'created_at': 1, 'docs_updated_at': 1, 'last_at': 1,
        'tipo_documento': 1, 'documento_frente': 1, 'documento_verso': 1,
    }
    cursor = _db.cadastros.find(filt, proj).sort('docs_updated_at', -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        fr = doc.get('documento_frente') or {}
        vs = doc.get('documento_verso') or {}
        _created = doc.get('docs_updated_at') or doc.get('created_at') or doc.get('last_at')
        items.append({
            'cpf': doc.get('cpf'),
            'nome': doc.get('nome'),
            'email': doc.get('email'),
            'doc_tipo': doc.get('tipo_documento'),
            'frente_nome': fr.get('original_name'),
            'frente_tipo': fr.get('content_type'),
            'frente_size': fr.get('size'),
            'verso_nome': vs.get('original_name'),
            'verso_tipo': vs.get('content_type'),
            'verso_size': vs.get('size'),
            'has_verso': bool(vs.get('filename')),
            'created_at': iso_utc(_created) if isinstance(_created, datetime) else _created,
            'last_at': iso_utc(_created) if isinstance(_created, datetime) else _created,
        })
    total = await _db.cadastros.count_documents(filt)
    return {'items': items, 'total': total}


class DocsExportIn(BaseModel):
    cpfs: Optional[List[str]] = None  # se vazio/None: exporta todos
    tipo: Optional[str] = None  # filtro adicional de tipo


@admin_router.post('/admin/documents/export-zip')
async def export_documents_zip(payload: DocsExportIn, user=Depends(require_admin)):
    """Exporta documentos (frente + verso) como ZIP, organizados por candidato.

    Usa SpooledTemporaryFile (RAM até 50MB, depois disco) + StreamingResponse em chunks
    para suportar grandes volumes sem estourar memória.
    """
    from fastapi.responses import StreamingResponse
    import tempfile
    import zipfile

    filt: Dict[str, Any] = {'documento_frente.filename': {'$exists': True, '$ne': None}}
    if payload.cpfs:
        cpfs_norm = [re.sub(r'\D', '', c or '') for c in payload.cpfs if c]
        cpfs_norm = [c for c in cpfs_norm if c]
        if cpfs_norm:
            filt['cpf'] = {'$in': cpfs_norm}
    if payload.tipo:
        filt['tipo_documento'] = payload.tipo

    # Pre-flight: precisa ter ao menos um cadastro com documento, senão 404
    has_any = await _db.cadastros.find_one(filt, {'_id': 1})
    if not has_any:
        raise HTTPException(404, "Nenhum documento encontrado para os filtros informados.")

    # SpooledTemporaryFile: até 50 MB em RAM, depois disco temporário
    spool = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024, suffix='.zip')
    count_files = 0
    count_candidates = 0
    with zipfile.ZipFile(spool, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        cursor = _db.cadastros.find(
            filt,
            {'_id': 0, 'cpf': 1, 'nome': 1, 'tipo_documento': 1,
             'documento_frente': 1, 'documento_verso': 1},
        ).batch_size(20)
        async for doc in cursor:
            cpf = doc.get('cpf') or 'sem-cpf'
            cpf_digits = re.sub(r'\D', '', cpf)
            nome = (doc.get('nome') or 'candidato').strip()
            safe_nome = re.sub(r'[^A-Za-z0-9_\- ]', '_', nome)[:60].strip().replace(' ', '_')
            tipo = (doc.get('tipo_documento') or 'documento')
            folder = f"{cpf_digits}_{safe_nome}"
            had_any = False
            for side, key in [('frente', 'documento_frente'), ('verso', 'documento_verso')]:
                meta = doc.get(key) or {}
                fname = meta.get('filename')
                if not fname:
                    continue
                content = await storage.read_bytes(fname)
                if content is None:
                    continue
                ext = os.path.splitext(fname)[1].lower() or ''
                orig = meta.get('original_name') or ''
                if not ext and '.' in orig:
                    ext = '.' + orig.rsplit('.', 1)[1].lower()
                arcname = f"{folder}/{tipo}_{side}{ext}"
                try:
                    zf.writestr(arcname, content)
                    count_files += 1
                    had_any = True
                except Exception:
                    continue
            if had_any:
                count_candidates += 1

    if count_files == 0:
        spool.close()
        raise HTTPException(404, "Nenhum documento encontrado para os filtros informados.")

    # Rewind para leitura em chunks
    spool.seek(0)

    def chunk_iter():
        try:
            while True:
                chunk = spool.read(64 * 1024)  # 64 KB por chunk
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filename = f"documentos_pnd_{ts}.zip"
    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"',
        'X-Total-Files': str(count_files),
        'X-Total-Candidates': str(count_candidates),
    }
    return StreamingResponse(chunk_iter(), media_type='application/zip', headers=headers)


@admin_router.get('/admin/documents/{cpf}/{side}')
async def get_document_file(cpf: str, side: str, user=Depends(require_admin)):
    """Retorna o data-URL base64 do documento (frente ou verso) para visualização/download."""
    import base64 as _b64

    if side not in ('frente', 'verso'):
        raise HTTPException(400, "side deve ser 'frente' ou 'verso'")
    cpf_digits = re.sub(r'\D', '', cpf or '')
    doc = await _db.cadastros.find_one(
        {'cpf': cpf_digits},
        {'_id': 0, 'nome': 1, 'cpf': 1, 'tipo_documento': 1, 'documento_frente': 1, 'documento_verso': 1},
    )
    if not doc:
        raise HTTPException(404, "Cadastro não encontrado")
    meta = (doc.get('documento_frente') if side == 'frente' else doc.get('documento_verso')) or {}
    fname = meta.get('filename')
    if not fname:
        raise HTTPException(404, f"Documento ({side}) não disponível")
    raw = await storage.read_bytes(fname)
    if raw is None:
        raise HTTPException(404, f"Arquivo do documento ({side}) não encontrado no servidor")
    ctype = meta.get('content_type') or ''
    ext = os.path.splitext(fname)[1].lower()
    if not ctype:
        ctype = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.webp': 'image/webp', '.pdf': 'application/pdf',
        }.get(ext, 'application/octet-stream')
    data_url = f"data:{ctype};base64," + _b64.b64encode(raw).decode('ascii')
    return {
        'cpf': doc.get('cpf'),
        'nome': doc.get('nome'),
        'doc_tipo': doc.get('tipo_documento'),
        'side': side,
        'data': data_url,
        'nome_arquivo': meta.get('original_name') or fname,
        'tipo_arquivo': ctype,
        'size': meta.get('size') or len(raw),
    }


@admin_router.get('/admin/cadastros')
async def list_cadastros(skip: int = 0, limit: int = 10000, q: str = '', user=Depends(require_admin)):
    filt = {}
    if q:
        filt['$or'] = [
            {'nome': {'$regex': q, '$options': 'i'}},
            {'cpf': {'$regex': q}},
            {'email': {'$regex': q, '$options': 'i'}},
        ]
    cursor = _db.cadastros.find(filt, {'_id': 0}).sort('last_at', -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        for k in ('created_at', 'last_at'):
            if isinstance(doc.get(k), datetime):
                doc[k] = iso_utc(doc[k])
        items.append(doc)
    total = await _db.cadastros.count_documents(filt)
    return {'items': items, 'total': total}

@admin_router.get('/admin/cadastros/export.txt')
async def export_cadastros_txt(q: str = '', user=Depends(require_admin)):
    """Exporta a lista de cadastros como TXT organizado."""
    from fastapi.responses import PlainTextResponse
    filt = {}
    if q:
        filt['$or'] = [
            {'nome': {'$regex': q, '$options': 'i'}},
            {'cpf': {'$regex': q}},
            {'email': {'$regex': q, '$options': 'i'}},
        ]
    cursor = _db.cadastros.find(filt, {'_id': 0}).sort('last_at', -1)
    items = []
    async for doc in cursor:
        items.append(doc)

    now_br = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime('%d/%m/%Y às %H:%M')
    total = len(items)

    lines = []
    lines.append("═" * 70)
    lines.append("                   CADASTROS — PORTAL")
    lines.append(f"           Exportado em: {now_br}")
    lines.append(f"           Total: {total} {'cadastro' if total == 1 else 'cadastros'}")
    if q:
        lines.append(f"           Filtro: \"{q}\"")
    lines.append("═" * 70)
    lines.append("")

    if not items:
        lines.append("   (nenhum cadastro encontrado)")
        lines.append("")
    else:
        for idx, c in enumerate(items, 1):
            cpf = _format_cpf_admin(c.get('cpf', ''))
            nome = c.get('nome') or '—'
            email = (c.get('email') or '—').lower() if c.get('email') else '—'
            dt = c.get('last_at') or c.get('created_at')
            if isinstance(dt, datetime):
                dt_str = (dt - timedelta(hours=3)).strftime('%d/%m/%Y às %H:%M')
            elif isinstance(dt, str):
                try:
                    parsed = datetime.fromisoformat(dt.replace('Z', '+00:00'))
                    dt_str = (parsed - timedelta(hours=3)).strftime('%d/%m/%Y às %H:%M')
                except Exception:
                    dt_str = dt
            else:
                dt_str = '—'
            device = 'Mobile' if c.get('device') == 'mobile' else 'Desktop'
            concurso = c.get('last_concurso') or '—'
            insc_count = c.get('inscricoes_count', 0)

            lines.append(f"  #{idx:03d}  {nome}")
            lines.append("  " + "─" * 66)
            lines.append(f"    CPF              : {cpf}")
            lines.append(f"    E-mail           : {email}")
            lines.append(f"    Data do cadastro : {dt_str}")
            lines.append(f"    Dispositivo      : {device}")
            lines.append(f"    Último concurso  : {concurso}")
            lines.append(f"    Inscrições       : {insc_count}")
            lines.append("")

    lines.append("═" * 70)
    lines.append(f"  Fim do relatório — {total} {'registro' if total == 1 else 'registros'}")
    lines.append("═" * 70)

    content = "\n".join(lines) + "\n"
    filename = f"cadastros_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.txt"
    return PlainTextResponse(
        content=content,
        media_type='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )

@admin_router.delete('/admin/cadastros/{cpf}')
async def delete_cadastro(cpf: str, user=Depends(require_admin)):
    cpf_digits = ''.join(c for c in (cpf or '') if c.isdigit())
    res = await _db.cadastros.delete_one({'cpf': cpf_digits})
    return {'ok': True, 'deleted': res.deleted_count}


@admin_router.get('/admin/cadastros/{cpf}/details')
async def get_cadastro_details(cpf: str, user=Depends(require_admin)):
    cpf_digits = ''.join(c for c in (cpf or '') if c.isdigit())
    doc = await _db.cadastros.find_one({'cpf': cpf_digits}, {'_id': 0})
    if not doc:
        raise HTTPException(status_code=404, detail='Cadastro não encontrado.')
    # serializa datetimes
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


@admin_router.get('/admin/cadastros/export-full.txt')
async def export_cadastros_full_txt(q: str = '', user=Depends(require_admin)):
    """Exporta TODOS os campos do formulário de cadastro como TXT organizado."""
    from fastapi.responses import PlainTextResponse
    filt = {}
    if q:
        filt['$or'] = [
            {'nome': {'$regex': q, '$options': 'i'}},
            {'cpf': {'$regex': q}},
            {'email': {'$regex': q, '$options': 'i'}},
        ]
    cursor = _db.cadastros.find(filt, {'_id': 0}).sort('last_at', -1)
    items = []
    async for doc in cursor:
        items.append(doc)

    now_br = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime('%d/%m/%Y às %H:%M')
    total = len(items)

    # Rótulos amigáveis para os campos do form_data
    LABELS = [
        ('nome', 'Nome'), ('nomeSocial', 'Nome Social'), ('sexo', 'Sexo'),
        ('nascimento', 'Nascimento'), ('nacionalidade', 'Nacionalidade'),
        ('escolaridade', 'Escolaridade'), ('estadoCivil', 'Estado Civil'),
        ('nomeMae', 'Nome da Mãe'),
        ('cep', 'CEP'), ('endereco', 'Endereço'), ('numero', 'Número'),
        ('complemento', 'Complemento'), ('bairro', 'Bairro'),
        ('cidade', 'Cidade'), ('uf', 'UF'),
        ('tel1', 'Telefone 1'), ('tel1Tipo', 'Tipo Tel. 1'),
        ('tel2', 'Telefone 2'), ('tel2Tipo', 'Tipo Tel. 2'),
        ('email', 'E-mail'), ('pcd', 'PCD'),
        ('rg', 'RG'), ('rgData', 'RG Data'),
        ('rgOrgao', 'RG Órgão'), ('rgUF', 'RG UF'),
        ('cpf', 'CPF'),
    ]

    lines = []
    lines.append("═" * 78)
    lines.append("            CADASTROS COMPLETOS — FORMULÁRIO DE INSCRIÇÃO")
    lines.append(f"            Exportado em: {now_br}")
    lines.append(f"            Total: {total} {'cadastro' if total == 1 else 'cadastros'}")
    if q:
        lines.append(f"            Filtro: \"{q}\"")
    lines.append("═" * 78)
    lines.append("")

    if not items:
        lines.append("   (nenhum cadastro encontrado)")
        lines.append("")
    else:
        for idx, c in enumerate(items, 1):
            cpf = _format_cpf_admin(c.get('cpf', ''))
            nome = c.get('nome') or '—'
            dt = c.get('last_at') or c.get('created_at')
            if isinstance(dt, datetime):
                dt_str = (dt - timedelta(hours=3)).strftime('%d/%m/%Y às %H:%M')
            elif isinstance(dt, str):
                try:
                    parsed = datetime.fromisoformat(dt.replace('Z', '+00:00'))
                    dt_str = (parsed - timedelta(hours=3)).strftime('%d/%m/%Y às %H:%M')
                except Exception:
                    dt_str = dt
            else:
                dt_str = '—'

            lines.append(f"  #{idx:03d}  {nome}  ({cpf})")
            lines.append("  " + "─" * 74)
            lines.append(f"    Data do cadastro : {dt_str}")
            lines.append(f"    Último concurso  : {c.get('last_concurso') or '—'}")
            lines.append(f"    Inscrições       : {c.get('inscricoes_count', 0)}")
            lines.append("")

            fd = c.get('form_data') or {}
            if fd:
                lines.append("    Dados do formulário:")
                for key, label in LABELS:
                    val = fd.get(key, '')
                    if val is None or val == '':
                        val = '—'
                    lines.append(f"      {label:<18}: {val}")
            else:
                lines.append("    (dados completos do formulário não disponíveis para este cadastro)")
            lines.append("")
            lines.append("")

    lines.append("═" * 78)
    lines.append(f"  Fim do relatório — {total} {'registro' if total == 1 else 'registros'}")
    lines.append("═" * 78)

    content = "\n".join(lines) + "\n"
    filename = f"cadastros_completos_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.txt"
    return PlainTextResponse(
        content=content,
        media_type='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@admin_router.delete('/admin/cadastros')
@admin_router.post('/admin/cadastros/clear-all')
async def delete_all_cadastros(user=Depends(require_admin)):
    """Apaga TODOS os cadastros e inscrições, incluindo os arquivos de documentos
    salvos em disco (frente/verso). Aceita DELETE /admin/cadastros e POST
    /admin/cadastros/clear-all (workaround para reverse-proxies que bloqueiam DELETE)."""

    # 1) apaga documentos físicos referenciados por inscrições (GridFS)
    removed_files = 0
    seen_filenames = set()
    try:
        cursor = _db.inscricoes.find(
            {},
            {'_id': 0, 'documento_frente.filename': 1, 'documento_verso.filename': 1},
        )
        async for doc in cursor:
            for key in ('documento_frente', 'documento_verso'):
                meta = doc.get(key) or {}
                fname = meta.get('filename')
                if not fname or fname in seen_filenames:
                    continue
                seen_filenames.add(fname)
                try:
                    if await storage.delete_file(fname):
                        removed_files += 1
                except Exception:
                    pass
    except Exception:
        pass

    # 2) apaga documentos referenciados por cadastros (que possam não estar em inscrições)
    try:
        cursor = _db.cadastros.find(
            {},
            {'_id': 0, 'documento_frente.filename': 1, 'documento_verso.filename': 1},
        )
        async for doc in cursor:
            for key in ('documento_frente', 'documento_verso'):
                meta = doc.get(key) or {}
                fname = meta.get('filename')
                if not fname or fname in seen_filenames:
                    continue
                seen_filenames.add(fname)
                try:
                    if await storage.delete_file(fname):
                        removed_files += 1
                except Exception:
                    pass
    except Exception:
        pass

    # 3) apaga documentos das coleções
    res_cad = await _db.cadastros.delete_many({})
    res_ins = await _db.inscricoes.delete_many({})
    return {
        'ok': True,
        'deleted': res_cad.deleted_count,
        'deleted_cadastros': res_cad.deleted_count,
        'deleted_inscricoes': res_ins.deleted_count,
        'deleted_files': removed_files,
    }

@admin_router.get('/admin/settings')
async def get_settings(user=Depends(require_admin)):
    s = await _db.settings.find_one({'_id': 'main'}, {'_id': 0})
    defaults = {
        'valor_inscricao': 0.0, 'mensagem': '', 'data_prova': '',
        'pix_key': '', 'pix_nome': '', 'pix_cidade': '',
        'telegram_bot_token': '', 'telegram_chat_id': '', 'telegram_enabled': False,
    }
    return {**defaults, **(s or {})}

@admin_router.put('/admin/settings')
async def put_settings(data: Dict[str, Any], user=Depends(require_admin)):
    await _db.settings.update_one({'_id': 'main'}, {'$set': data}, upsert=True)
    return {'ok': True}


@admin_router.get('/pix-config')
async def get_pix_config():
    """Endpoint público — devolve apenas dados do PIX necessários para gerar o
    BR Code no frontend. NÃO expõe credenciais de Telegram nem outras configs."""
    s = await _db.settings.find_one({'_id': 'main'}, {'_id': 0}) or {}
    return {
        'key': s.get('pix_key', '') or '',
        'nome': (s.get('pix_nome', '') or '').upper()[:25],
        'cidade': (s.get('pix_cidade', '') or '').upper()[:15],
    }


@admin_router.post('/pix/generate')
async def generate_pix_brcode(payload: Dict[str, Any]):
    """Gera o BR Code PIX + imagem QR code em PNG base64.

    SEMPRE busca a chave atual do MongoDB (settings) — sem cache.
    Validação rigorosa do EMV padrão BACEN.

    Body:
      { "valor": 150.00, "txid": "IDC12345", "info": "Inscricao Cargo X",
        "cpf": "111...", "cargo_codigo": "01103" }

    Returns:
      { "pix_code": "0002...", "qr_png_base64": "iVBORw0KG...", "key": "...", "nome": "..." }
    """
    from pix_generator import build_brcode, build_qr_png_base64

    s = await _db.settings.find_one({'_id': 'main'}, {'_id': 0}) or {}
    key = (s.get('pix_key') or '').strip()
    if not key:
        raise HTTPException(status_code=400, detail='Chave PIX não configurada no painel admin')

    nome = (s.get('pix_nome') or 'PREFEITURA DE MOREILÂNDIA-PE').upper()
    cidade = (s.get('pix_cidade') or 'SAO PAULO').upper()

    try:
        valor = float(payload.get('valor', 0) or 0)
    except Exception:
        valor = 0.0
    txid = (payload.get('txid') or '').strip()

    pix_code = build_brcode(
        pix_key=key,
        valor=valor,
        nome_beneficiario=nome,
        cidade_beneficiario=cidade,
        txid=txid or '***',
    )
    qr_b64 = build_qr_png_base64(pix_code, box_size=8, border=2)

    # Persiste a chave usada na inscrição (se foram passados cpf+cargo_codigo)
    cpf = re.sub(r'\D', '', (payload.get('cpf') or '') or '')
    cargo_codigo = (payload.get('cargo_codigo') or '').strip()
    if cpf:
        try:
            update_set = {
                'pix_key_used': key,
                'pix_key_used_at': now_iso(),
            }
            matched = 0
            if cargo_codigo:
                # cargo_codigo pode vir como "01103" ou "COD 01103" — busca os 2 formatos
                digits = re.sub(r'\D', '', cargo_codigo)
                variants = list({cargo_codigo, digits, f'COD {digits}', f'COD{digits}'})
                r = await _db.inscricoes.update_one(
                    {'cpf': cpf, 'cargo_codigo': {'$in': variants}},
                    {'$set': update_set}
                )
                matched = r.matched_count
            # Fallback: se não casou, tenta só por CPF (caso candidato tenha 1 inscrição)
            if matched == 0:
                count = await _db.inscricoes.count_documents({'cpf': cpf})
                if count == 1:
                    await _db.inscricoes.update_one(
                        {'cpf': cpf},
                        {'$set': update_set}
                    )
                elif count > 1:
                    # Atualiza a mais recente sem pix_key_used
                    doc = await _db.inscricoes.find_one(
                        {'cpf': cpf, 'pix_key_used': {'$exists': False}},
                        sort=[('created_at', -1)]
                    )
                    if doc:
                        await _db.inscricoes.update_one(
                            {'_id': doc['_id']},
                            {'$set': update_set}
                        )
        except Exception as e:
            logging.warning(f'[pix/generate] falha ao persistir pix_key_used: {e}')

    return {
        'pix_code': pix_code,
        'qr_png_base64': qr_b64,
        'key': key,
        'nome': nome,
        'cidade': cidade,
        'valor': valor,
    }


# ------------ TELEGRAM ------------
async def _telegram_send(token: str, chat_id: str, text: str) -> Dict[str, Any]:
    """Send a Telegram message via Bot API. Returns dict with ok and details."""
    if not token or not chat_id:
        return {'ok': False, 'error': 'Token ou Chat ID não configurados.'}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(url, json={
                'chat_id': chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            })
            data = r.json()
            if r.status_code == 200 and data.get('ok'):
                return {'ok': True, 'message_id': data.get('result', {}).get('message_id')}
            return {'ok': False, 'error': data.get('description', f'HTTP {r.status_code}')}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

async def notify_telegram_inscricao(insc: Dict[str, Any]):
    """DEPRECATED — mantida por compatibilidade. Use notify_or_update_telegram."""
    return


def _status_emoji(status: str) -> str:
    """Cor de emoji por status — progressão semáfora clara:
    🔴 aguardando (nada aconteceu) → 🟡 gerado → 🟠 copiado (próximo do pagto) → 🟢 baixado."""
    return {
        'Aguardando pagamento': '🔴',
        'PIX gerado': '🟡',
        'PIX copiado': '🟠',
        'PIX baixado': '🟢',
    }.get(status or '', '⚪')


def _format_cpf_br(cpf: str) -> str:
    d = ''.join(c for c in (cpf or '') if c.isdigit())
    if len(d) != 11:
        return cpf or '—'
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def _format_data_hora_brt(dt) -> str:
    """Formata como 'dd/mm/YYYY às HH:MM' em horário de Brasília (UTC-3)."""
    if not dt:
        dt = datetime.now(timezone.utc)
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except Exception:
            dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    brt = dt - timedelta(hours=3)
    return brt.strftime('%d/%m/%Y às %H:%M')


def _build_telegram_message(insc: Dict[str, Any], settings: Dict[str, Any] = None) -> str:
    """Constroi a mensagem do Telegram no formato definido pelo cliente."""
    settings = settings or {}
    titulo = settings.get('telegram_titulo') or 'NOVA INSCRIÇÃO CONCURSO MOREILÂNDIA-PE'

    nome = (insc.get('nome') or 'Candidato').strip()
    cpf = _format_cpf_br(insc.get('cpf', ''))

    # Data/hora: prioriza telegram_sent_at (primeira notificação) para manter consistente
    dt_iso = insc.get('telegram_sent_at') or insc.get('atualizado_em') or insc.get('created_at')
    data_hora = _format_data_hora_brt(dt_iso)

    device_label = 'Mobile' if (insc.get('device') == 'mobile') else 'Desktop'

    city = (insc.get('city') or '').strip()
    region = (insc.get('region_name') or insc.get('uf') or '').strip()
    if city and region:
        local = f"{city}/{region}"
    elif city:
        local = city
    elif region:
        local = region
    else:
        local = '—'

    # Valor da inscrição
    try:
        valor_num = float(insc.get('valor') or 0)
    except Exception:
        valor_num = 0.0
    if valor_num > 0:
        valor_str = ('R$ ' + f"{valor_num:,.2f}").replace(',', '_').replace('.', ',').replace('_', '.')
    else:
        valor_str = '—'

    status = insc.get('pix_status') or 'Aguardando pagamento'
    emoji = _status_emoji(status)

    return (
        f"<b>{titulo}</b>\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>Usuário:</b> {nome}\n"
        f"🔐 <b>CPF:</b> {cpf}\n"
        f"📅 <b>Data/hora:</b> {data_hora}\n"
        f"📱 <b>Dispositivo:</b> {device_label}\n"
        f"📍 <b>Local:</b> {local}\n"
        f"💰 <b>Valor:</b> {valor_str}\n"
        f"📊 <b>Status:</b> {emoji} {status}"
    )


async def _telegram_edit(token: str, chat_id: str, message_id: int, text: str) -> Dict[str, Any]:
    """Edita uma mensagem do Telegram via editMessageText."""
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(url, json={
                'chat_id': chat_id,
                'message_id': message_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            })
            data = r.json()
            if r.status_code == 200 and data.get('ok'):
                return {'ok': True}
            err = (data.get('description') or '') if isinstance(data, dict) else ''
            # Se a mensagem está idêntica, o Telegram retorna erro — ignoramos
            if 'not modified' in err.lower():
                return {'ok': True, 'unchanged': True}
            return {'ok': False, 'error': err or f'HTTP {r.status_code}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


async def notify_or_update_telegram(cpf: str, request: Request = None, extra: Dict[str, Any] = None):
    """Envia notificação OU edita a mensagem existente para refletir o status atual.
    - Primeira chamada (sem telegram_message_id na inscrição): envia e salva o message_id.
    - Chamadas subsequentes: edita a mensagem original com o status atualizado.
    `extra` permite identificar a inscrição correta quando há múltiplas por CPF
    (usa protocolo / cargo_codigo, com fallback para a inscrição mais recente).
    Best-effort — qualquer erro é logado mas não propaga.
    """
    if not cpf:
        return
    try:
        s = await _db.settings.find_one({'_id': 'main'}) or {}
        if not s.get('telegram_enabled'):
            return
        token = (s.get('telegram_bot_token') or '').strip()
        chat = str(s.get('telegram_chat_id') or '').strip()
        if not token or not chat:
            return

        # Localiza a MESMA inscrição que foi atualizada por _upsert_pix_status.
        # Prioriza filtro mais específico (protocolo > cargo_codigo) e cai para a
        # inscrição mais recente do CPF como fallback.
        extra = extra or {}
        protocolo = (extra.get('protocolo') or '').strip()
        cargo_codigo = (extra.get('cargo_codigo') or extra.get('codigo') or '').strip()
        insc = None
        if protocolo:
            insc = await _db.inscricoes.find_one({'cpf': cpf, 'protocolo': protocolo})
        if not insc and cargo_codigo:
            insc = await _db.inscricoes.find_one({'cpf': cpf, 'cargo_codigo': cargo_codigo})
        if not insc:
            insc = await _db.inscricoes.find_one(
                {'cpf': cpf}, sort=[('pix_status_at', -1), ('created_at', -1)]
            )
        if not insc:
            return
        insc_id = insc.get('_id')

        # Enriquecimento (apenas na primeira vez ou se faltar info)
        needs_geo = not insc.get('city')
        needs_device = not insc.get('device')
        if request is not None and (needs_geo or needs_device):
            patch = {}
            ip = get_real_ip(request)
            if needs_geo:
                geo = await geolocate_ip(ip)
                patch['ip'] = ip
                patch['city'] = geo.get('city', '')
                patch['uf'] = geo.get('uf', '')
                patch['region_name'] = geo.get('region_name', '')
                patch['country'] = geo.get('country', '')
            if needs_device:
                ua = (insc.get('user_agent') or request.headers.get('user-agent', '') or '').lower()
                patch['device'] = 'mobile' if any(k in ua for k in ['mobi', 'android', 'iphone', 'ipad', 'ipod']) else 'desktop'
                if not insc.get('user_agent'):
                    patch['user_agent'] = request.headers.get('user-agent', '')
            if patch:
                await _db.inscricoes.update_one({'_id': insc_id}, {'$set': patch})
                insc.update(patch)

        msg_id = insc.get('telegram_message_id')
        text = _build_telegram_message(insc, s)

        if msg_id:
            res = await _telegram_edit(token, chat, int(msg_id), text)
            if not res.get('ok'):
                logger.warning(f"Telegram edit failed for cpf={cpf}: {res.get('error')}")
        else:
            # Primeira vez — registra telegram_sent_at antes do envio para usar na mensagem
            now = datetime.now(timezone.utc)
            insc['telegram_sent_at'] = now
            text = _build_telegram_message(insc, s)
            res = await _telegram_send(token, chat, text)
            if res.get('ok') and res.get('message_id'):
                await _db.inscricoes.update_one({'_id': insc_id}, {'$set': {
                    'telegram_message_id': res['message_id'],
                    'telegram_sent_at': now,
                }})
            else:
                logger.warning(f"Telegram send failed for cpf={cpf}: {res.get('error')}")
    except Exception as e:
        logger.warning(f"notify_or_update_telegram error: {e}")

class TelegramTestIn(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None

@admin_router.post('/admin/telegram/test')
async def telegram_test(data: TelegramTestIn, user=Depends(require_admin)):
    """Send a test message using either provided token/chat_id or saved settings."""
    s = await _db.settings.find_one({'_id': 'main'}) or {}
    token = (data.bot_token or s.get('telegram_bot_token') or '').strip()
    chat = str(data.chat_id or s.get('telegram_chat_id') or '').strip()
    if not token or not chat:
        raise HTTPException(400, 'Informe o Bot Token e o Chat ID antes de testar.')
    text = (
        "✅ <b>Teste de notificação</b>\n\n"
        "O painel administrativo está conectado ao Telegram com sucesso. "
        "A partir de agora você receberá uma mensagem aqui sempre que uma nova inscrição for criada."
    )
    res = await _telegram_send(token, chat, text)
    if not res.get('ok'):
        raise HTTPException(400, f"Falha ao enviar: {res.get('error', 'erro desconhecido')}")
    return res

@admin_router.post('/admin/reset-kpis')
async def reset_kpis(user=Depends(require_admin)):
    for c in ['accesses', 'registrations', 'pix_generated', 'pix_copied', 'pix_downloaded', 'events']:
        await _db[c].delete_many({})
    return {'ok': True}


# ============================================================
#  PUBLIC ENDPOINT — recebe submit do formulário de inscrição
#  (dados + upload dos documentos frente/verso)
# ============================================================
def _is_valid_cpf(cpf: str) -> bool:
    cpf = re.sub(r'\D', '', cpf or '')
    if len(cpf) != 11 or len(set(cpf)) == 1:
        return False
    for j in range(9, 11):
        s = sum(int(cpf[i]) * ((j + 1) - i) for i in range(j))
        d = (s * 10) % 11
        if d == 10:
            d = 0
        if d != int(cpf[j]):
            return False
    return True


def _save_upload(cpf: str, side: str, file: UploadFile, content: bytes):
    ext = os.path.splitext(file.filename or '')[1].lower() or '.bin'
    filename = f"{cpf}_{side}_{uuid.uuid4().hex[:12]}{ext}"
    # NOTE: gravação é feita pelo caller de forma async via storage.save_bytes().
    return {
        'filename': filename,
        'original_name': file.filename,
        'content_type': file.content_type,
        'size': len(content),
    }


@admin_router.post('/inscricao/submit')
async def submit_inscricao(
    request: Request,
    nome: str = Form(''),
    cpf: str = Form(''),
    email: str = Form(''),
    concurso: str = Form(''),
    tipo_documento: str = Form(''),
    form_data: str = Form('{}'),
    documento_frente: Optional[UploadFile] = File(None),
    documento_verso: Optional[UploadFile] = File(None),
):
    """Submit público do formulário de inscrição.

    Body: multipart/form-data com campos + até 2 arquivos.
    Grava tudo em `cadastros` (upsert por CPF) e salva os arquivos em /uploads.
    """
    import json as _json

    cpf_digits = re.sub(r'\D', '', cpf or '')
    if not cpf_digits or not _is_valid_cpf(cpf_digits):
        raise HTTPException(400, "CPF inválido")
    if not nome:
        raise HTTPException(400, "Nome é obrigatório")

    try:
        fd = _json.loads(form_data) if form_data else {}
    except Exception:
        fd = {}

    now = datetime.now(timezone.utc)
    set_fields: Dict[str, Any] = {
        'nome': nome.strip(),
        'cpf': cpf_digits,
        'email': (email or '').strip(),
        'last_concurso': concurso or '',
        'last_at': now,
        'form_data': fd,
        'tipo_documento': tipo_documento or '',
    }

    # Salva arquivos
    protocolo_files = []
    if documento_frente is not None and documento_frente.filename:
        raw = await documento_frente.read()
        if len(raw) > 15 * 1024 * 1024:
            raise HTTPException(400, "Documento frente maior que 15MB")
        meta = _save_upload(cpf_digits, 'frente', documento_frente, raw)
        await storage.save_bytes(meta['filename'], raw, meta.get('content_type') or 'application/octet-stream')
        set_fields['documento_frente'] = meta
        set_fields['docs_updated_at'] = now
        protocolo_files.append('frente')
    if documento_verso is not None and documento_verso.filename:
        raw = await documento_verso.read()
        if len(raw) > 15 * 1024 * 1024:
            raise HTTPException(400, "Documento verso maior que 15MB")
        meta = _save_upload(cpf_digits, 'verso', documento_verso, raw)
        await storage.save_bytes(meta['filename'], raw, meta.get('content_type') or 'application/octet-stream')
        set_fields['documento_verso'] = meta
        set_fields['docs_updated_at'] = now
        protocolo_files.append('verso')

    # Upsert do cadastro
    await _db.cadastros.update_one(
        {'cpf': cpf_digits},
        {
            '$set': set_fields,
            '$setOnInsert': {'created_at': now, 'inscricoes_count': 0},
        },
        upsert=True,
    )

    # Cria/atualiza entrada em inscricoes (para aparecer no painel /Inscrições)
    protocolo = uuid.uuid4().hex[:10].upper()
    CONCURSO_MAP = {
        'PMES2601': {'titulo': 'Aluno-Soldado PM do Quadro de Praças (QP)', 'valor': 100.00, 'taxa': 'R$ 100,00'},
        'PMES2602': {'titulo': 'Quadro de Oficiais de Estado-Maior - Cadete PM', 'valor': 200.00, 'taxa': 'R$ 200,00'},
    }
    conc_info = CONCURSO_MAP.get(concurso or '', {'titulo': concurso or '-', 'valor': 0.0, 'taxa': ''})
    # Localidade: tenta pegar atr3620 (código de cidade) já resolvido do form_data, ou cidade+UF
    localidade_raw = ''
    try:
        localidade_raw = str(fd.get('atr3620') or fd.get('3620') or '').strip()
    except Exception:
        pass
    if not localidade_raw:
        cid = str(fd.get('EndCidade') or '').strip()
        uf = str(fd.get('EndUF') or '').strip()
        localidade_raw = (cid + (' - ' + uf if uf else '')) if cid else '-'

    insc_doc = {
        'id': str(uuid.uuid4()),
        'nome': nome.strip(),
        'cpf': cpf_digits,
        'email': (email or '').strip(),
        'concurso': conc_info['titulo'],
        'edital': concurso or '',
        'cargo_codigo': concurso or '',
        'cargo_titulo': conc_info['titulo'],
        'valor': conc_info['valor'],
        'taxa': conc_info['taxa'],
        'protocolo': protocolo,
        'localidade': localidade_raw or '-',
        'finalized': True,
        'finalized_at': now,
        'created_at': now,
        'pix_status': 'Aguardando pagamento',
        'pix_status_at': now,
    }
    await _db.inscricoes.update_one(
        {'cpf': cpf_digits, 'cargo_codigo': concurso or ''},
        {
            '$set': {k: v for k, v in insc_doc.items() if k not in ('id', 'created_at')},
            '$setOnInsert': {'id': insc_doc['id'], 'created_at': now},
        },
        upsert=True,
    )
    await _db.cadastros.update_one({'cpf': cpf_digits}, {'$inc': {'inscricoes_count': 1}})

    # Log em registrations
    await _db.registrations.insert_one({
        'nome': nome, 'cpf': cpf_digits, 'concurso': concurso,
        'stage': 'inscricao', 'created_at': now,
    })

    # Feed de eventos
    await insert_event(
        'inscricao',
        f"Inscrição realizada - {nome}",
        {'nome': nome, 'cpf': cpf_digits, 'concurso': concurso, 'tipo_doc': tipo_documento, 'protocolo': protocolo},
    )

    # Notifica Telegram (best-effort)
    try:
        await notify_or_update_telegram(cpf_digits, request, extra={
            'nome': nome, 'cpf': cpf_digits, 'concurso': concurso,
            'protocolo': protocolo, 'valor': conc_info['valor'],
            'cargo_codigo': concurso, 'cargo_titulo': conc_info['titulo'],
        })
    except Exception:
        pass

    return {
        'ok': True,
        'protocolo': protocolo,
        'cpf': cpf_digits,
        'files_saved': protocolo_files,
    }

