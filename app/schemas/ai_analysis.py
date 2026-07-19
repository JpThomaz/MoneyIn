from typing import List, Optional
from pydantic import BaseModel, Field


class TransacaoResponse(BaseModel):
    data: str = ""
    transaction_day: Optional[int] = None
    transaction_month: Optional[int] = None
    hora: Optional[str] = None
    descricao: str
    description_clean: Optional[str] = None
    valor: float
    saldo_parcial: Optional[float] = None
    categoria: Optional[str] = None
    installment_number: int = 1
    total_installments: int = 1
    reference_month: Optional[int] = None
    reference_year: Optional[int] = None


class AnaliseFinanceiraResponse(BaseModel):
    documento_valido: bool
    codigo_erro: Optional[str] = None
    tipo_documento: Optional[str] = None
    banco_identificado: Optional[str] = None
    quatro_ultimos_digitos: Optional[str] = None
    saldo_final_extrato: Optional[float] = None
    mes_vencimento: Optional[int] = None
    ano_vencimento: Optional[int] = None
    transacoes: List[TransacaoResponse] = Field(default_factory=list)
