# PRD — Site Consulpam + Painel Admin

## Original Problem Statement
Clone de repositório GitHub (FastAPI + React + MongoDB). Preservar painel admin `/donaspainel` + backend; substituir site público pelo site clonado da Consulpam (Concurso Público — Prefeitura Municipal de Moreilândia-PE — Edital 001/2026). Integrar formulários públicos ao backend (cadastros, tracking, PIX, Telegram).

## Idioma do Usuário
Português (pt-BR)

## Arquitetura
- **Backend**: FastAPI + MongoDB (`/api/*`).
- **Frontend público**: HTML estáticos em `/app/frontend/public/*.html`, roteados via middleware do `craco.config.js`.
- **Painel Admin**: React estático buildado em `/app/frontend/public/donaspainel/` — preservado do repo original.
- **Integrações**: Telegram Bot API (notificações), PIX EMV (geração de código).

## Páginas Públicas
- `/` — index.html (home)
- `/inscricao` — inscricao.html (formulário)
- `/confirmar-dados` — confirmar-dados.html (revisão)
- `/comprovante` — comprovante.html (recibo)
- `/pagamento` — pagamento.html (PIX + QR)

## Credenciais
- Admin `/donaspainel`: `donas` / `Seinao10@@`

## Implementado
- **[20/02/2026]** Pente-fino pré-VPS: removidos backups antigos em `/app/tmp/` (`logoin.html`, `pg2.html`, `pg1.html`, `header_original.html` que continham "SOBRAL-CE" / "GUARDA CIVIL"). Atualizado `DEPLOY_VPS.md` (DB_NAME `pmsp_producao` → `moreilandia_producao`, serviço `PMSP Backend` → `Moreilandia Backend`, rotas `/inscricao-pmsp2601.html` e `/inscricao-pmsp2602.html` → `/inscricao`, título de notificações Telegram `PM SP` → `MOREILÂNDIA-PE`). Atualizados `.oxlintignore` (raiz e frontend) removendo pastas legadas `farpainel/farpapainel`. Refatorados testes de regressão em `test_vps_export_audit.py` para nomes neutros. PRD.md atualizado. Consulpam mantido (é a banca real que organiza o concurso de Moreilândia-PE).
- **[20/02/2026]** Página `/pagamento`: removido o botão "Imprimir Pagamento" (e o listener associado ao tracking `pix-downloaded`). Restam apenas ← Voltar e Copiar PIX.
- **[20/02/2026]** Corrigido flash de dados-placeholder em `/comprovante` e `/confirmar-dados`: conteúdo dinâmico agora inicia com `visibility:hidden` e ganha a classe `.ready` somente após o JS injetar os valores da sessionStorage — elimina o FOUC entre reload e dados reais.
- **[20/02/2026]** Página `/comprovante`: removidos os campos Código, Identidade, Orgão Emissor, Se enquadra na Lei 12.990/14, Se enquadra no Decreto 3.298/99, e No dia da prova. Cargo e CPF agora ocupam a largura total.
- **[20/02/2026]** Página `/confirmar-dados`: dados reorganizados em 4 seções com cabeçalhos azuis (Dados Pessoais, Endereço, Cargo Escolhido, Informações Adicionais). Texto do radio de aceite alterado para "Estou de acordo. Irei efetuar o pagamento da taxa de inscrição **dentro do prazo estabelecido por esta banca**".
- **[19/07/2026]** Correção de responsividade mobile: CSS `<style id="mobile-fix">` refinado nos 5 HTMLs. Aplicado em `@media (max-width:768px)` e `(max-width:480px)`. Cobertura: header, menu, tabelas com `width` fixo (500/948), fieldsets, inputs, footer com endereço. Testado em 390x844 (iPhone) — sem scroll horizontal, formulários legíveis, botões full-width.
- Rebranding Prefeitura Municipal de Moreilândia-PE (dashboard).
- Modal home com logo oficial.
- Integração completa `POST /api/inscricoes/submit`, tracking PIX (generated/copied/downloaded), Telegram notifications.
- PIX BR Code gerado via `pix_generator.py`.
- Botão "voltar" na pág. pagamento; cabeçalho oficial em todas as pág.

## Backlog / Próximos Passos (P2)
- Fluxo end-to-end de teste real com submissão de inscrição + geração PIX + notificação Telegram.
- Melhorar UX do menu (agora scrollável horizontalmente no mobile).
- Testar em tablet (768px) — pode precisar breakpoint intermediário.
