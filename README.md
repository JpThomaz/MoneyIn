# MoneyIn

Sistema de gerenciamento financeiro familiar com motor preditivo baseado em regressao harmonica sazonal.

## Funcionalidades

- **Cadastro e autenticacao** de usuarios e households (unidades familiares)
- **Importacao e categorizacao** de transacoes financeiras
- **Dashboard** com visao consolidada de receitas, despesas e saldo
- **Motor preditivo** de projetcao de fluxo de caixa (3-6 meses) usando regressao harmônica sazonal por OLS
- **Analise de residuos** e diagnostico estatistico (Ljung-Box, Shapiro-Wilk, ACF/PACF)
- **Chat financeiro** integrado com IA generativa (Google Gemini)

## Pre-requisitos

- Python >= 3.13
- [uv](https://docs.astral.sh/uv/) (gerenciador de pacotes)

## Instalacao

```bash
# Clonar o repositorio
git clone https://github.com/seu-usuario/moneyin.git
cd moneyin

# Instalar dependencias com uv
uv sync

# Copiar o arquivo de configuracao de ambiente
cp .env.example .env
# Editar .env com suas chaves de API (GEMINI_API_KEY, JWT_SECRET, etc.)
```

## Uso

### Web API (FastAPI)

```bash
# Iniciar o servidor de desenvolvimento
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

O servidor estara disponivel em `http://localhost:8000`.

### Notebook de Analise Preditiva

```bash
# Iniciar JupyterLab
uv run jupyter lab
```

Abrir o notebook `notebooks/analise_preditiva_sazonal.ipynb` e executar todas as celulas em sequencia.

#### Celulas do Notebook

| # | Celula | Descricao |
|---|--------|-----------|
| 1 | Imports | Carrega bibliotecas e configura estilo visual |
| 2 | Carregamento de dados | Le transacoes do banco SQLite e constroi serie mensal |
| 3 | Decomposicao temporal | Decompoe a serie em tendencia, sazonalidade e residuos |
| 4 | White noise | Testa se residuos sao ruido branco (Ljung-Box) |
| 5 | Classe HarmonicRegression | Implementacao OLS com Gauss-Jordan (Python puro) |
| 6 | Diagnostico de residuos | Analise completa (9 graficos: QQ-Plot, ACF, PACF, etc.) |
| 7 | Walk-forward validation | Validacao cruzada passo-a-frente sem vazamento de dados |
| 8 | Grafico de entrega | Previsao final com intervalo de confianca 95% |
| 9 | Figura 3 | Grafico descritivo de residuos (estilo white paper) |

## Metricas do Modelo

Resultados obtidos com serie de 12 meses (Jul/2025 - Jun/2026):

| Metrica | Valor |
|---------|-------|
| MAE medio | R$ 5.444,20 |
| RMSE medio | R$ 6.146,58 |
| MAPE medio | 270,43% |
| Ljung-Box p_min | 0,1077 |

> **Nota:** O MAPE elevado e inflado por valores proximos de zero e mudancas de sinal na serie. MAE e RMSE sao metricas mais representativas.

## Figuras Geradas

As figuras sao salvas automaticamente durante a execucao do notebook:

| Figura | Arquivo | Descricao |
|--------|---------|-----------|
| Decomposicao temporal | `figs/decomposicao_temporal.png` | Serie decomposta em tendencia, sazonalidade e residuos |
| White noise | `figs/white_noise_analysis.png` | Histograma, QQ-Plot, ACF e PACF dos residuos da decomposicao |
| Diagnostico completo | `figs/residual_diagnostics.png` | 9 graficos de diagnostico do modelo harmonico |
| Previsao final | `figs/previsao_final.png` | Historico + projecao com IC 95% e residuos |
| Figura 3 (white paper) | `notebooks/figura3_residuos_ruido_branco.png` | Residuos, densidade e ACF em formato 1x3 |

## Estrutura do Projeto

```
moneyin/
├── app/                    # Aplicacao FastAPI
│   ├── api/routers/        # Endpoints REST
│   ├── core/               # Configuracao e database
│   ├── models/             # Modelos SQLModel
│   ├── schemas/            # Schemas Pydantic
│   ├── services/           # Logica de negocio
│   └── templates/          # Templates Jinja2
├── data/                   # Dados de exemplo
├── figs/                   # Figuras geradas
├── notebooks/              # Jupyter notebooks
│   └── analise_preditiva_sazonal.ipynb
├── moneyin.db              # Banco SQLite
├── pyproject.toml          # Configuracao uv
└── relatorio_tecnico_sbc_finep.md  # Relatorio tecnico
```

## Casos de Uso

### 1. Previsao de Fluxo de Caixa Familiar

Familias com renda variavel podem usar o motor preditivo para antecipar periodos de saldo negativo e tomar decisoes preventivas (adiamento de compras, negociacao de prazos).

### 2. Planejamento Financeiro Mensal

O dashboard permite visualizar a evolucao de receitas e despesas ao longo do tempo, identificando tendencias de gasto e oportunidades de economia.

### 3. Analise de Sazonalidade

A decomposicao temporal revela padroes ciclicos (decimo terceiro ferias, férias escolares) que influenciam o fluxo de caixa, permitindo planejamento antecipado.

### 4. Diagnostico Estatistico

Os testes de normalidade (Shapiro-Wilk), autocorrelacao (Ljung-Box) e homocedasticidade garantem que o modelo e confiavel para tomada de decisao.

## Tecnologias

- **Backend:** FastAPI, SQLModel, Uvicorn
- **Banco de Dados:** SQLite
- **Ciencia de Dados:** pandas, numpy, scipy, scikit-learn, statsmodels, matplotlib, seaborn
- **IA:** Google Gemini (chat financeiro)
- **Gerenciamento de Pacotes:** uv
