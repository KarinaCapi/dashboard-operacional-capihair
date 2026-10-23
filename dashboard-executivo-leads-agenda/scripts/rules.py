"""Regras de classificação da agenda do Bitrix (aprovadas pela gerência).

Funções puras, sem rede e sem dados reais. Usadas por extract.py e pelos testes.
"""
import re
import unicodedata

# Cor do evento -> unidade (regra aprovada: vale a cor, não a SDR).
UNIT_BY_COLOR = {
    '#0092cc': 'BOTAFOGO', '#3ba7ef': 'BOTAFOGO',
    '#de2b24': 'BARRA', '#c21b16': 'BARRA',
    '#e89b06': 'GOIANIA',
    '#fcaa1c': 'GOIANIA',  # cor nova: tratada como Goiânia (a confirmar pela gerência)
}
PROVISIONAL_COLORS = {'#fcaa1c'}

# Criador do evento (ID Bitrix) -> SDR.
SDR_BY_CREATOR = {'35': 'Joyce', '17': 'Fabiana', '15': 'Waleska', '19': 'Luana', '11': 'Jordana'}
# Criadores fora do escopo de leads.
OUT_CREATOR = {
    '41': 'closers (lista de closers)',
    '71': 'social selling (Mayara)',
    '81': 'criador não identificado',
}

STATUSES = ['CONFIRMADA', 'PENDENTE', 'REALIZADA', 'FECHOU', 'NÃO FECHOU', 'VETADO',
            'FALTOU', 'DESMARCOU', 'REMARCADO', 'FECHOU EM OUTRA CLÍNICA']
ATTENDED = ('REALIZADA', 'FECHOU', 'NÃO FECHOU', 'VETADO')


def nrm(s):
    return unicodedata.normalize('NFD', s or '').encode('ascii', 'ignore').decode().lower()


def status_from_title(title):
    """Status inferido do título; None quando o título não traz status."""
    n = nrm(title)
    if 'fechou em outra' in n:
        return 'FECHOU EM OUTRA CLÍNICA'
    if re.search(r'\b(nao|n)\s+fechou|\bnaofechou', n):
        return 'NÃO FECHOU'
    if re.search(r'fechou|fechado', n):
        return 'FECHOU'
    if 'realizad' in n:
        return 'REALIZADA'
    if 'vetad' in n:
        return 'VETADO'
    if 'falt' in n:
        return 'FALTOU'
    if 'desmarc' in n:
        return 'DESMARCOU'
    if re.search(r'remarcou|remarcado', n):
        return 'REMARCADO'
    # palavras definidas pela gerência como FALTOU
    if re.search(r'nao veio|nao atende|nao responde|n responde|imprevisto|enrolado no trabalho|tentar vir ate o final do dia', n):
        return 'FALTOU'
    # palavras definidas pela gerência como REALIZADA (avaliação feita, em negociação)
    if re.search(r'negociando|forma de pagamento|buscando forma', n):
        return 'REALIZADA'
    if re.search(r'\btrat\b', n):  # TRAT conta como avaliação
        return 'REALIZADA'
    if 'confirmad' in n:
        return 'CONFIRMADA'
    return None


def tags(title):
    n = nrm(title)
    t = set()
    if re.search(r'\bind\b|\bind\.|indica', n):
        t.add('IND')
    if re.search(r'\bss\b', n):
        t.add('SS')
    if re.search(r'\bret\b|\bret\.', n):
        t.add('RET')
    if re.search(r'\btrat\b', n):
        t.add('TRAT')
    return t


def scope(creator, title, overrides_out=False):
    """('LEAD'|'IND'|'FORA', motivo)."""
    if overrides_out:
        return 'FORA', 'decisão da gerência'
    if creator in OUT_CREATOR:
        return 'FORA', OUT_CREATOR[creator]
    t = tags(title)
    if 'SS' in t:
        return 'FORA', 'tag SS (social selling)'
    if 'IND' in t:
        return 'IND', 'indicação (número à parte)'
    return 'LEAD', 'no escopo'


def classify(event, overrides=None):
    """event: dict(id, date(ISO), color, creator, title). Retorna dict classificado.

    overrides (opcional, vem de segredo, nunca do repositório):
      {"<id>": {"status": "FALTOU", "fora": true}}
    """
    ov = (overrides or {}).get(str(event['id']), {})
    color = (event.get('color') or '').lower()
    unit = UNIT_BY_COLOR.get(color)
    kind, motivo = scope(str(event.get('creator')), event.get('title', ''), bool(ov.get('fora')))
    st = ov.get('status') or status_from_title(event.get('title', '')) or 'PENDENTE'
    return dict(id=str(event['id']), d=event['date'], unit=unit, color=color,
                sdr=SDR_BY_CREATOR.get(str(event.get('creator'))), kind=kind, motivo=motivo,
                status=st, titled=status_from_title(event.get('title', '')) is not None,
                provisional_color=color in PROVISIONAL_COLORS)


def rates(statuses):
    """Taxas pela definição aprovada. statuses: lista de status dos eventos."""
    prev = len(statuses)
    el = [s for s in statuses if s not in ('DESMARCOU', 'REMARCADO')]
    att = [s for s in el if s in ATTENDED]
    fal = [s for s in el if s == 'FALTOU']
    return dict(previstos=prev, elegiveis=len(el), compareceram=len(att), faltou=len(fal),
                comparecimento=round(100 * len(att) / len(el), 1) if el else None,
                noshow=round(100 * len(fal) / len(el), 1) if el else None)
