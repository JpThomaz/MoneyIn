# Instruções do Copilot - Projeto "Money In"

## 1. Visão Geral e Dor do Negócio
O **Money In** é uma plataforma inteligente de gestão financeira com foco inicial na dinâmica familiar. 
* **O Problema:** O atrito e a preguiça do preenchimento manual de despesas causam o abandono do controle financeiro.
* **A Solução:** Ingestão de dados automatizada via extração de texto de PDFs (extratos/faturas), processada por IA, combinada com dashboards preditivos e conselhos financeiros automatizados.
* **O Objetivo Final:** Prover clareza sobre o caminho para a "Independência Financeira", respeitando orçamentos e evitando redundâncias financeiras.

## 2. Stack Tecnológica
* **Backend:** Python 3.11+ com **FastAPI**.
* **ORM e Banco de Dados:** **SQLModel**. A arquitetura deve suportar SQLite (ambiente de desenvolvimento local) e PostgreSQL (ambiente de produção), configurável via variável de ambiente `DATABASE_URL`.
* **Leitura de PDF:** `PyMuPDF` (biblioteca `fitz`) para extração super rápida de texto bruto no backend.
* **Frontend:** Next.js (React) focado em UI/UX limpa, cores calmas (azul escuro, verde menta), e gráficos de alta performance.
* **IA/GenAI:** SDK Oficial do Google (`google-genai`) usando modelos Gemini Flash-Lite via Free Tier.

## 3. Regras de Negócio Core: Dinâmica Familiar (Multi-tenant)
* **Entidade Familiar (`Household`):** O centro do sistema não é o usuário, mas a família. Todo usuário pertence a um `household_id`.
* **Perfis e Convites:** Um usuário cria a família (Admin) e gera um link/código de convite para adicionar a esposa/cônjuge ao mesmo `household_id`.
* **Visibilidade Isolada e Integrada:** As tabelas de transações e contas bancárias devem pertencer a um usuário específico, mas vinculadas ao `household_id`. Isso permite que o Dashboard filtre visualizações: "Meus Gastos", "Gastos Dela(e)" e "Visão Consolidada da Família".

## 4. Pipeline de Ingestão e Processamento (O Motor)
1. **Upload e Extração:** O endpoint recebe múltiplos PDFs. O `PyMuPDF` extrai o texto bruto instantaneamente.
2. **Parsing via LLM:** O texto bruto é enviado ao Gemini com um `System Prompt` restrito e um `response_schema` (Pydantic). O Gemini atua estritamente como conversor de texto para JSON padronizado.
3. **Idempotência (Antiduplicidade):** Geração de hash `SHA256(data + valor_absoluto + descricao_normalizada)`. Se o hash existir no banco para aquele `household_id`, a transação é ignorada. Permite uploads sobrepostos sem medo.
4. **Resolução de Transferências Internas:** Se uma saída de R$ X na conta do Usuário A e uma entrada de R$ X na conta do Usuário B (ou na mesma conta) ocorrerem em um intervalo de até 2 dias úteis, o sistema vincula ambas como `Tipo: Transferencia` para não inflar as despesas/receitas reais.

## 5. Dashboards, Previsibilidade e Inteligência de Dados
* **Dashboards (O que deve ser renderizado):**
  * Calendário mensal com dias marcados em verde/vermelho (receitas vs gastos).
  * Gráficos de categorias (pizza/barras) e padrões de consumo recorrente.
  * Módulo de Investimentos: Acompanhamento simples de aportes e lucros/perdas, focado no cálculo percentual rumo ao número mágico da Independência Financeira.
* **Previsões (Machine Learning Clássico):**
  * Uso de regressão linear simples ou ARIMA (local no Python) para calcular o *Burn Rate* (ritmo de gasto) do mês.
  * **Alerta de Teto:** "No ritmo atual, você estourará o orçamento de 'Lazer' no dia 22 do mês".
* **Conselheiro Financeiro (IA Generativa):**
  * **Regra de Custo:** A IA **nunca** lerá o banco de dados inteiro.
  * O backend criará um resumo agregado em JSON (Ex: `{"mes": "Maio", "receita_total": 15000, "gasto_fixo": 8000, "gasto_lazer_aumento_percentual": 15, "meta_poupanca_atingida": false}`).
  * Esse mini-JSON é enviado ao Gemini para gerar uma "Opinião da IA", ex: *"Notei que os gastos com aplicativos de entrega subiram 15% este mês. Se mantivermos a meta de poupança intacta, recomendo segurar os pedidos nesse fim de semana."*

## 6. Diretrizes de Código para o Copilot
* Utilize o conceito de injeção de dependência (`Depends`) do FastAPI para a sessão do banco de dados (SQLModel) e verificação do `household_id` do usuário autenticado.
* Separe a lógica em: `routers` (endpoints), `services` (regras de negócio e chamadas de IA), `models` (SQLModel) e `schemas` (Pydantic base).