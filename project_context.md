# Contexto do Projeto: Money In (Finanças Familiares)

## 1. Visão do Produto
O **Money In** é uma plataforma de controle financeiro automatizado para casais. O objetivo é eliminar o preenchimento manual de despesas através do upload de arquivos textuais e PDFs (extratos/faturas), convertidos por IA (Gemini Flash-Lite) em transações estruturadas de forma instantânea. O app é construído como um monolito rápido e elegante usando Python.

## 2. Stack Tecnológica Atual
* **Backend:** Python 3.11+ com **FastAPI** e **SQLModel**.
* **Banco de Dados:** SQLite (Desenvolvimento) / PostgreSQL (Produção).
* **Frontend:** **HTMX** (para requisições assíncronas sem refresh) + **Tailwind CSS** (via CDN) + **Jinja2 Templates** + **ApexCharts** (Gráficos).

## 3. Regras de Negócio Implementadas
* **Multi-tenancy Familiar:** Usuários são vinculados a um `household_id` (Família). O casal compartilha a visão financeira.
* **Antiduplicidade:** Transações geram um Hash SHA256 único e são ignoradas se já existirem no banco.
* **Filtro de Transferências:** O sistema anula movimentações internas entre contas do casal automaticamente.

## 4. Requisito Visual (UI/UX Premium)
O frontend não deve parecer um relatório de dados, mas sim uma plataforma de software de nível corporativo (SaaS Dashboard). 
* **Layout:** Sidebar lateral de navegação fixa, barra superior (Topbar) com gerenciador de perfil/família, área de notificações e Grid de conteúdo responsivo.
* **Cores:** Fundo limpo (Slate/Zinc 50), menus escuros e sóbrios (Slate 900), realces em Verde Menta (Emerald 500) para ganhos e Vermelho Pastel (Rose 500) para gastos.