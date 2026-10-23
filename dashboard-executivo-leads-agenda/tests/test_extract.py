"""Testes com dados SINTÉTICOS (nenhum dado real). Execute: python -m unittest discover -s tests"""
import datetime as dt, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import rules, extract

class Rules(unittest.TestCase):
    def test_status_words(self):
        for t, s in [('Fulano v4 Joyce - não responde', 'FALTOU'), ('X imprevisto tentar vir até o final do dia', 'FALTOU'),
                     ('X enrolado no trabalho', 'FALTOU'), ('X negociando', 'REALIZADA'), ('X buscando forma de pagamento', 'REALIZADA'),
                     ('X NÃO FECHOU', 'NÃO FECHOU'), ('X fechou', 'FECHOU'), ('Marion fechou', 'FECHOU'), ('X vetado', 'VETADO'),
                     ('X desmarcou', 'DESMARCOU'), ('X TRAT v4', 'REALIZADA'), ('X só marcado', None)]:
            self.assertEqual(rules.status_from_title(t), s, t)
    def test_scope_unit(self):
        e = dict(id=1, date='2026-10-17', color='#DE2B24', creator='35', title='Fulano v4 anuncio / Joyce')
        c = rules.classify(e); self.assertEqual((c['unit'], c['kind'], c['sdr'], c['status']), ('BARRA', 'LEAD', 'Joyce', 'PENDENTE'))
        self.assertEqual(rules.classify(dict(e, title='Fulano ind Joyce'))['kind'], 'IND')
        self.assertEqual(rules.classify(dict(e, creator='41'))['kind'], 'FORA')
        self.assertEqual(rules.classify(dict(e, creator='81'))['kind'], 'FORA')
        self.assertEqual(rules.classify(dict(e, title='Fulano SS'))['kind'], 'FORA')
        self.assertEqual(rules.classify(dict(e, color='#fcaa1c'))['unit'], 'GOIANIA')
        self.assertEqual(rules.classify(e, {'1': {'status': 'FALTOU'}})['status'], 'FALTOU')
        self.assertEqual(rules.classify(e, {'1': {'fora': True}})['kind'], 'FORA')
    def test_rates(self):
        r = rules.rates(['REALIZADA', 'FECHOU', 'FALTOU', 'FALTOU', 'DESMARCOU', 'REMARCADO', 'PENDENTE', 'CONFIRMADA'])
        self.assertEqual((r['previstos'], r['elegiveis'], r['compareceram'], r['faltou']), (8, 6, 2, 2))
        self.assertEqual((r['comparecimento'], r['noshow']), (33.3, 33.3))

class Pipeline(unittest.TestCase):
    def setUp(self):
        self.kmap = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'config', 'kommo_map.json'), encoding='utf-8'))
        self.tmp = tempfile.mkdtemp(); extract.ROOT = self.tmp
        os.makedirs(os.path.join(self.tmp, 'config')); os.makedirs(os.path.join(self.tmp, 'data'))
        json.dump(self.kmap, open(os.path.join(self.tmp, 'config', 'kommo_map.json'), 'w'))
        os.environ.update(KOMMO_TOKEN='x', BITRIX_WEBHOOK_URL='https://example.invalid/rest/1/abc/')
        fab = '14238387'; st = self.kmap['pipelines'][fab]['stages']
        ids = list(st)
        self.created = int(dt.datetime(2026, 10, 14, 12, tzinfo=extract.TZ).timestamp())
        self.leads = [dict(id=i + 1, pipeline_id=int(fab), status_id=int([ids[1], ids[3], ids[4]][i % 3]), created_at=self.created) for i in range(6)]
        self.ind_stage = [s for s, v in st.items() if v['ind']][0]
        ev = [dict(ID='1', NAME='A v4 Fabiana faltou', DATE_FROM='15.10.2026 09:00:00', COLOR='#0092cc', CREATED_BY='17'),
              dict(ID='2', NAME='B ind Fabiana', DATE_FROM='15.10.2026 10:00:00', COLOR='#0092cc', CREATED_BY='17'),
              dict(ID='3', NAME='C closer', DATE_FROM='15.10.2026 11:00:00', COLOR='#0092cc', CREATED_BY='41'),
              dict(ID='4', NAME='D v4 Joyce fechou', DATE_FROM='16.10.2026 11:00:00', COLOR='#de2b24', CREATED_BY='35')]
        self.ev = ev
        def fake(url, headers=None, retries=4):
            if '/api/v4/leads?' in url:
                return {'_embedded': {'leads': self.leads if 'filter%5Bpipeline_id%5D%5B%5D=14238387' in url else []}, '_links': {}}
            if '/api/v4/events' in url:
                return {'_embedded': {'events': [dict(entity_type='lead', entity_id=2, value_after=[{'lead_status': {'id': int(self.ind_stage)}}])]}, '_links': {}}
            if 'calendar.event.get' in url:
                return {'result': self.ev}
            raise AssertionError(url)
        extract.http_get_json = fake; extract.time.sleep = lambda *_: None
    def test_ok(self):
        s = extract.run('publish', dt.date(2026, 10, 19))
        d = json.load(open(os.path.join(self.tmp, 'data', 'data.json')))
        self.assertEqual(sum(r[5] for r in d['KM']), 6)
        self.assertEqual(sum(r[5] for r in d['KM'] if r[3] == 'IND'), 1)  # lead 2 passou pela etapa Indicação
        self.assertEqual(d['META']['lead_ag'], 2); self.assertEqual(d['META']['ind_ag'], 1); self.assertEqual(d['META']['fora'], {'closers (lista de closers)': 1})
    def test_unknown_color_fails_and_keeps_last_valid(self):
        extract.run('publish', dt.date(2026, 10, 19)); p = os.path.join(self.tmp, 'data', 'data.json'); before = open(p).read()
        self.ev[0]['COLOR'] = '#123456'
        with self.assertRaises(extract.Fail): extract.run('publish', dt.date(2026, 10, 19))
        self.assertEqual(open(p).read(), before)
    def test_check_mode_writes_nothing(self):
        extract.run('check', dt.date(2026, 10, 19)); self.assertFalse(os.path.exists(os.path.join(self.tmp, 'data', 'data.json')))
    def test_history_preserved(self):
        hist = dict(ate='2026-10-16', AG=[['2026-10-15', 'BOTAFOGO', 'Fabiana', 'LEAD', 'FALTOU', 7]])
        json.dump(hist, open(os.path.join(self.tmp, 'data', 'history_frozen.json'), 'w'))
        extract.run('publish', dt.date(2026, 10, 19)); d = json.load(open(os.path.join(self.tmp, 'data', 'data.json')))
        self.assertIn(['2026-10-15', 'BOTAFOGO', 'Fabiana', 'LEAD', 'FALTOU', 7], d['AG'])
        self.assertTrue(all(r[0] <= '2026-10-16' for r in d['AG'] if r[2] == 'Fabiana' and r[5] == 7))

if __name__ == '__main__': unittest.main()
