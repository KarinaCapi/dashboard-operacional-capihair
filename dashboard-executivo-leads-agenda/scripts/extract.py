#!/usr/bin/env python3
"""Extração diária do Painel Executivo – Leads, Agenda e No-show (Capi Hair).

SOMENTE LEITURA nos sistemas: apenas requisições HTTP GET (Kommo API v4 e
calendar.event.get do Bitrix24). Nada é criado, alterado ou apagado nos CRMs.

Segredos (variáveis de ambiente, nunca impressos):
  KOMMO_TOKEN          token de longa duração do Kommo
  BITRIX_WEBHOOK_URL   URL do webhook de entrada do Bitrix24 (termina em /)
Opcionais:
  KOMMO_SUBDOMAIN      padrão: capihair
  OVERRIDES_JSON       decisões manuais por evento {"<id>": {"status": "...", "fora": true}}
                       (vem de segredo; ids/decisões individuais não ficam no repositório)

Saída (somente agregados – sem nomes, telefones, IDs de paciente ou de lead):
  data/data.json     dados do painel
  data/status.json   resultado da última execução (ok/falha + avisos)

Se qualquer verificação (gate) falhar, data.json NÃO é tocado: a última versão
válida é mantida e status.json registra a falha, exibida no painel.
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TZ = ZoneInfo('America/Sao_Paulo')
KOMMO_START = dt.date(2026, 10, 13)      # primeira data da base
BITRIX_ROLLING_DAYS = 14                  # janela móvel de recálculo do Bitrix
SDR_ORDER = ['Fabiana', 'Jordana', 'Luana', 'Joyce', 'Waleska']


class Fail(Exception):
    """Falha de verificação: mantém a última versão válida."""


# ----------------------------------------------------------------- HTTP (GET)
def http_get_json(url, headers=None, retries=4, fonte='API'):
    """GET com tentativas. Nunca inclui URL/cabeçalhos em mensagens de erro."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=dict(headers or {}, **{'Accept': 'application/json'}), method='GET')
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                if r.status == 204 or not body:
                    return None
                return json.loads(body)
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code == 204:
                return None
            if e.code in (401, 403):
                det = ''
                try:  # motivo informado pela API (texto curto; não contém credenciais)
                    j = json.loads(e.read() or b'{}')
                    det = str(j.get('detail') or j.get('hint') or j.get('title') or '')[:80]
                except Exception:
                    pass
                raise Fail(f'{fonte}: acesso negado ({e.code}){" - " + det if det else ""}; verifique o segredo e a permissão de leitura') from None
            time.sleep(2 + 3 * i)
        except Exception as e:  # rede, JSON inválido (ex.: HTML de limite de taxa)
            last = type(e).__name__
            time.sleep(2 + 3 * i)
    raise Fail(f'{fonte}: requisição falhou após {retries} tentativas ({last})')


# ---------------------------------------------------------------------- Kommo
class Kommo:
    def __init__(self, sub, token):
        self.base = f'https://{sub}.kommo.com'
        tk = token.strip()
        if tk.lower().startswith('bearer '):
            tk = tk[7:].strip()
        self.h = {'Authorization': 'Bearer ' + tk}

    def pages(self, path, params, key, limit=250, pause=0.25):
        page = 1
        while True:
            q = urllib.parse.urlencode(params, doseq=True)
            d = http_get_json(f'{self.base}{path}?{q}&limit={limit}&page={page}', self.h, fonte='Kommo')
            time.sleep(pause)
            items = ((d or {}).get('_embedded') or {}).get(key) or []
            yield from items
            if not items or len(items) < limit or not ((d or {}).get('_links') or {}).get('next'):
                return
            page += 1


def ts(date):
    return int(dt.datetime.combine(date, dt.time(0, 0), TZ).timestamp())


def extract_kommo(kommo, kmap, start, end_excl):
    """Retorna linhas agregadas [dia, sdr, unidade, V4|IND, etapa, n] e metadados."""
    pipes = kmap['pipelines']
    leads = {}
    for pid in pipes:
        for l in kommo.pages('/api/v4/leads', {
                'filter[pipeline_id][]': pid,
                'filter[created_at][from]': ts(start), 'filter[created_at][to]': ts(end_excl) - 1}, 'leads'):
            if l['id'] in leads:
                raise Fail('lead duplicado na paginação do Kommo')
            leads[l['id']] = (str(l['pipeline_id']), str(l['status_id']), l['created_at'])
    # Origem Indicação = etapa atual é Indicação OU o lead já entrou numa etapa Indicação.
    ind_stage = {sid for p in pipes.values() for sid, s in p['stages'].items() if s['ind']}
    ever_ind = set()
    for e in kommo.pages('/api/v4/events', {'filter[type][]': 'lead_status_changed',
                                            'filter[created_at][from]': ts(start)}, 'events', limit=100):
        if e.get('entity_type') != 'lead' or e.get('entity_id') not in leads:
            continue
        for v in e.get('value_after') or []:
            ls = v.get('lead_status') or {}
            if str(ls.get('id')) in ind_stage:
                ever_ind.add(e['entity_id'])
    rows = collections.Counter()
    unmapped = collections.Counter()
    for lid, (pid, sid, created) in leads.items():
        p = pipes[pid]
        stg = p['stages'].get(sid)
        if stg is None:
            name = f'Etapa não mapeada ({sid})'
            unmapped[name] += 1
        else:
            name = stg['name']
        day = dt.datetime.fromtimestamp(created, TZ).date().isoformat()
        orig = 'IND' if (stg and stg['ind']) or lid in ever_ind else 'V4'
        rows[(day, p['sdr'], p['unit'], orig, name)] += 1
    out = [list(k) + [n] for k, n in sorted(rows.items())]
    if sum(r[5] for r in out) != len(leads):
        raise Fail('soma das classificações do Kommo difere do total de leads')
    return out, dict(total=len(leads), unmapped=dict(unmapped))


# --------------------------------------------------------------------- Bitrix
def parse_dt(s):
    for f in ('%d.%m.%Y %H:%M:%S', '%d/%m/%Y %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%d %H:%M:%S', '%d.%m.%Y'):
        try:
            return dt.datetime.strptime(s, f).date()
        except (ValueError, TypeError):
            pass
    raise Fail('data de evento do Bitrix em formato desconhecido')


def extract_bitrix(webhook, start, end_excl, overrides):
    base = webhook.rstrip('/') + '/'
    ev = {}
    d = start
    while d < end_excl:
        e = min(d + dt.timedelta(days=6), end_excl - dt.timedelta(days=1))
        q = urllib.parse.urlencode({'type': 'company_calendar', 'ownerId': 0,
                                    'from': d.isoformat(), 'to': e.isoformat()})
        r = http_get_json(f'{base}calendar.event.get.json?{q}', fonte='Bitrix')
        if r is None or 'result' not in r:
            raise Fail('resposta inesperada do Bitrix (sem "result")')
        for x in r['result']:
            if x.get('DELETED') in ('Y', True):
                continue
            ev[str(x['ID'])] = dict(id=x['ID'], date=parse_dt(x['DATE_FROM']).isoformat(), color=x.get('COLOR'),
                                    creator=x.get('CREATED_BY'), title=x.get('NAME', ''))
        d = e + dt.timedelta(days=1)
        time.sleep(0.3)
    out = []
    for x in ev.values():
        if not (start.isoformat() <= x['date'] < end_excl.isoformat()):
            continue  # evento de dia anterior que atravessa a janela
        out.append(rules.classify(x, overrides))
    return out


def aggregate_bitrix(cls):
    cnt = collections.Counter()
    for c in cls:
        if c['kind'] == 'FORA':
            cnt[(c['d'], c['unit'] or '—', c['motivo'], 'FORA', 'FORA')] += 1
        else:
            sdr = c['sdr'] or '—'
            cnt[(c['d'], c['unit'], sdr, c['kind'], c['status'])] += 1
    return [list(k) + [n] for k, n in sorted(cnt.items(), key=lambda kv: tuple(map(str, kv[0])))]


# ---------------------------------------------------------------------- Gates
def gates_bitrix(cls):
    msgs = []
    if len({c['id'] for c in cls}) != len(cls):
        raise Fail('IDs duplicados no Bitrix')
    sem = [c for c in cls if c['kind'] != 'FORA' and not c['unit']]
    if sem:
        raise Fail(f'{len(sem)} evento(s) no escopo com cor não mapeada (definir unidade antes de atualizar)')
    agg = aggregate_bitrix(cls)
    if sum(r[5] for r in agg) != len(cls):
        raise Fail('soma dos agregados do Bitrix difere do total de eventos')
    pend = sum(1 for c in cls if c['status'] == 'PENDENTE' and c['kind'] != 'FORA')
    if pend:
        msgs.append(f'{pend} agendamento(s) sem status no título (pendentes, mantidos no denominador)')
    prov = sum(1 for c in cls if c['provisional_color'])
    if prov:
        msgs.append(f'{prov} evento(s) com cor provisória (#FCAA1C tratada como Goiânia; confirmar)')
    return msgs


def compare_history(cls_hist, hist_rows):
    """Reconciliação: recomputa o período congelado a partir do Bitrix e compara com a base aprovada."""
    now = collections.Counter({(r[0], r[1], r[2], r[3], r[4]): r[5] for r in aggregate_bitrix(cls_hist) if r[3] != 'FORA'})
    old = collections.Counter({(r[0], r[1], r[2], r[3], r[4]): r[5] for r in hist_rows if r[3] != 'FORA'})
    diff = {k: (old.get(k, 0), now.get(k, 0)) for k in set(now) | set(old) if now.get(k, 0) != old.get(k, 0)}
    return len(diff), sum(old.values()), sum(now.values())


# ------------------------------------------------------------------------ main
def load_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def run(mode, today):
    data_dir = os.path.join(ROOT, 'data')
    os.makedirs(data_dir, exist_ok=True)
    kmap = load_json(os.path.join(ROOT, 'config', 'kommo_map.json'))
    prev = load_json(os.path.join(data_dir, 'data.json'))
    hist = load_json(os.path.join(data_dir, 'history_frozen.json'))
    hist_end = dt.date.fromisoformat(hist['ate']) if hist else None
    overrides = json.loads(os.environ.get('OVERRIDES_JSON') or '{}')
    token, hook = (os.environ.get('KOMMO_TOKEN') or '').strip(), (os.environ.get('BITRIX_WEBHOOK_URL') or '').strip()
    if not token or not hook:
        raise Fail('segredos KOMMO_TOKEN e/ou BITRIX_WEBHOOK_URL não configurados')
    end_excl = today  # só dias encerrados (< hoje)
    avisos = []

    # Kommo: foto atual de todos os leads criados desde o início da base.
    try:
        krows, kmeta = extract_kommo(Kommo(os.environ.get('KOMMO_SUBDOMAIN', 'capihair'), token), kmap, KOMMO_START, end_excl)
    except Fail as e:
        if 'Kommo: acesso negado' in str(e):  # forma do token (nunca o valor): ajuda a achar erro de colagem
            raise Fail(f'{e} [token com {len(token)} caracteres em {token.count(".") + 1} parte(s)]') from None
        raise
    if kmeta['total'] == 0:
        raise Fail('Kommo retornou 0 leads')
    if prev and kmeta['total'] < 0.9 * prev['META']['kommo_total']:
        raise Fail('total de leads do Kommo caiu mais de 10% em relação à versão publicada')
    if kmeta['unmapped']:
        avisos.append('etapas do Kommo sem mapeamento por ID: ' + ', '.join(f'{k}={v}' for k, v in kmeta['unmapped'].items()))

    # Bitrix: histórico congelado + dados já publicados + janela móvel recalculada.
    first_new = (hist_end + dt.timedelta(days=1)) if hist_end else KOMMO_START
    win_start = max(first_new, end_excl - dt.timedelta(days=BITRIX_ROLLING_DAYS))
    try:
        cls = extract_bitrix(hook, win_start, end_excl, overrides)
    except Fail as e:
        if str(e).startswith('Bitrix'):  # forma da URL (nunca o valor): ajuda a achar erro de colagem
            p = hook.split('/')
            raise Fail(f'{e} [segredo com {len(hook)} caracteres; começa com https://: {"sim" if hook.startswith("https://") else "não"}; '
                       f'contém /rest/: {"sim" if "/rest/" in hook else "não"}; termina com /: {"sim" if hook.endswith("/") else "não"}; '
                       f'partes separadas por /: {len(p)}]') from None
        raise
    avisos += gates_bitrix(cls)
    ag_new = aggregate_bitrix(cls)
    ag = []
    if hist:
        ag += [r for r in hist['AG']]
    if prev:
        ag += [r for r in prev['AG'] if (not hist_end or r[0] > hist_end.isoformat()) and r[0] < win_start.isoformat()]
    ag += ag_new
    recon = None
    if hist:  # reconciliação do período congelado contra o Bitrix atual (informativa)
        chk = extract_bitrix(hook, KOMMO_START, hist_end + dt.timedelta(days=1), overrides)
        recon = compare_history(chk, hist['AG'])
        avisos.append(f'reconciliação do período congelado: {recon[0]} célula(s) diferentes '
                      f'(base aprovada {recon[1]} × Bitrix hoje {recon[2]} agendamentos); a base aprovada prevalece')

    fora = collections.Counter()
    for r in ag:
        if r[3] == 'FORA':
            fora[r[2]] += r[5]
    meta = dict(kommo_snapshot=dt.datetime.now(TZ).strftime('%d/%m/%Y %H:%M BRT'),
                bitrix_leitura=dt.datetime.now(TZ).strftime('%d/%m/%Y %H:%M BRT'),
                kommo_total=kmeta['total'], fora=dict(fora),
                lead_ag=sum(r[5] for r in ag if r[3] == 'LEAD'), ind_ag=sum(r[5] for r in ag if r[3] == 'IND'),
                historico_congelado_ate=hist['ate'] if hist else None, janela_bitrix_recalculada_desde=win_start.isoformat(),
                avisos=avisos)
    out = dict(AG=ag, KM=krows, META=meta)
    status = dict(ok=True, quando=meta['kommo_snapshot'], avisos=avisos)
    summary = dict(kommo_leads=kmeta['total'], bitrix_eventos_janela=len(cls), avisos=len(avisos),
                   recon_celulas_diferentes=None if recon is None else recon[0])
    if mode == 'publish':
        write_json(os.path.join(data_dir, 'data.json'), out)
        write_json(os.path.join(data_dir, 'status.json'), status)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['check', 'publish'], default='check',
                    help='check: executa e valida sem gravar; publish: grava data/data.json')
    ap.add_argument('--today', help='AAAA-MM-DD (padrão: hoje em America/Sao_Paulo)')
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(TZ).date()
    try:
        s = run(a.mode, today)
        # Somente contagens de verificação no log (o log do Actions é público neste repositório).
        print('GATES: PASS')
        print(json.dumps(s, ensure_ascii=False))
    except Fail as e:
        msg = str(e)
        print('GATES: FAIL -', msg)
        # Mantém data.json (última versão válida); registra a falha para o painel exibir o aviso.
        if a.mode == 'publish':
            path = os.path.join(ROOT, 'data', 'status.json')
            old = load_json(path, {}) or {}
            write_json(path, dict(ok=False, quando=dt.datetime.now(TZ).strftime('%d/%m/%Y %H:%M BRT'),
                                  erro=msg, ultima_valida=old.get('ultima_valida') or old.get('quando')))
        sys.exit(1)
    except Exception as e:  # nunca imprime detalhes (poderiam conter URL/segredo)
        print('GATES: FAIL - erro inesperado:', type(e).__name__)
        sys.exit(2)


if __name__ == '__main__':
    main()
