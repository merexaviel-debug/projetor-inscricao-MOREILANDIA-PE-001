"""VPS export audit - smoke tests before exporting repo to VPS."""
import os
import re
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"User-Agent": "vps-audit-test/1.0"})
    return sess


def test_api_root(s):
    r = s.get(f"{BASE_URL}/api/", timeout=15)
    assert r.status_code == 200
    assert "Painel Administrativo" in r.text


def test_home_modal_text(s):
    r = s.get(f"{BASE_URL}/", timeout=15)
    assert r.status_code == 200
    expected = "As inscrições para o Concurso Público da Prefeitura Municipal de Moreilândia - PE - Edital 001/2026 - Cargos Gerais encerram-se dia 21/09/2026, às 23h59 (horário de Brasília)."
    # Strip HTML tags to compare plain text (source has <b> tags inline)
    stripped = re.sub(r"<[^>]+>", "", r.text)
    stripped_norm = re.sub(r"\s+", " ", stripped)
    assert expected in stripped_norm, f"Expected modal text missing. Excerpt: {stripped_norm[stripped_norm.find('As inscrições'):stripped_norm.find('As inscrições')+300] if 'As inscrições' in stripped_norm else 'not found'}"
    # Extract modal block only (id="aviso-modal-backdrop" to its closing)
    modal_match = re.search(r'aviso-modal-backdrop.*?</div>\s*</div>', r.text, re.DOTALL)
    modal_html = modal_match.group(0) if modal_match else ""
    modal_stripped = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", modal_html))
    assert "REABERTAS" not in modal_stripped, f"Modal still contains REABERTAS: {modal_stripped}"
    assert "20/07" not in modal_stripped


def test_inscricao_html(s):
    r = s.get(f"{BASE_URL}/inscricao.html", timeout=15)
    assert r.status_code == 200
    # Should contain CPF field
    assert re.search(r"cpf", r.text, re.IGNORECASE)


def test_comprovante_text(s):
    r = s.get(f"{BASE_URL}/comprovante", timeout=15)
    assert r.status_code == 200
    expected = "Obs: Sua inscrição só será efetivada após a confirmação do pagamento da taxa de inscrição pela instituição financeira, podendo levar até 24 horas."
    assert expected in r.text


def test_admin_login_donas(s):
    r = s.post(f"{BASE_URL}/api/admin/auth/login",
               json={"username": "donas", "password": "Seinao10@@"}, timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "token" in data or "access_token" in data
    user = data.get("user", {})
    assert user.get("username") == "donas"


def test_admin_login_legacy_removed(s):
    """Regressão: credenciais legadas de projetos anteriores devem retornar 401."""
    r = s.post(f"{BASE_URL}/api/admin/auth/login",
               json={"username": "legacy_admin", "password": "legacy_password"}, timeout=15)
    assert r.status_code == 401


def test_donaspainel_route(s):
    r = s.get(f"{BASE_URL}/donaspainel", timeout=15)
    assert r.status_code == 200
    # Should serve admin SPA bundle
    assert "/donainel/static/js/main.fda9cfa5.js" in r.text


def test_legacy_admin_route_no_admin(s):
    """Regressão: rota legada não deve servir o bundle admin."""
    r = s.get(f"{BASE_URL}/legacy-admin-panel", timeout=15, allow_redirects=True)
    # Should NOT serve admin bundle. Acceptable: 404 or 200 with public home.
    if r.status_code == 200:
        assert "/donainel/static/js/main" not in r.text, "legacy route is still serving admin bundle!"
    else:
        assert r.status_code in (404, 301, 302)


def test_admin_protected_endpoint_requires_jwt(s):
    r = s.get(f"{BASE_URL}/api/admin/dashboard/kpis", timeout=15)
    assert r.status_code in (401, 403)


def test_admin_protected_endpoint_with_jwt(s):
    login = s.post(f"{BASE_URL}/api/admin/auth/login",
                   json={"username": "donas", "password": "Seinao10@@"}, timeout=15)
    assert login.status_code == 200
    token = login.json().get("token") or login.json().get("access_token")
    assert token
    r = s.get(f"{BASE_URL}/api/admin/dashboard/kpis",
              headers={"Authorization": f"Bearer {token}"}, timeout=20)
    assert r.status_code == 200
