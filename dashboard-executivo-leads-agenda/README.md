# Painel Executivo – Leads, Agenda e No-show (Capi Hair)

Painel diário para a diretoria: **Leads** (Kommo), **Agendamentos** e **No-show** (agenda do Bitrix24).
Esta pasta é independente dos demais painéis do repositório.

## Estado atual
**Preparado, NÃO publicado, automação DESATIVADA.** O workflow só roda manualmente. Não há dados reais nesta pasta.

## Segurança (leia antes de ativar)
- **O repositório é público.** Tudo que for commitado é público: só entram **agregados** (contagens por dia, filial, SDR, etapa e status).
  Nunca entram nomes, telefones, IDs de paciente/lead, descrições de eventos, planilhas, tokens ou URLs de webhook.
- **Não há senha no frontend.** Uma senha em HTML/JS não protege dados. Se os números não puderem ser públicos,
  o caminho correto é hospedagem com acesso restrito (repositório privado + hospedagem autenticada), e não uma senha na página.
- **Segredos** (Settings → Secrets and variables → Actions), nunca em arquivos, logs ou chat:

  | Nome | Uso |
  |---|---|
  | `KOMMO_TOKEN` | token de longa duração do Kommo (o código só usa `GET`) |
  | `BITRIX_WEBHOOK_URL` | URL do webhook de entrada do Bitrix24 (só `calendar.event.get`) |
  | `OVERRIDES_JSON` *(opcional)* | decisões manuais por evento; fica fora do repositório |

  Variáveis (não secretas): `KOMMO_SUBDOMAIN` (padrão `capihair`) e `PUBLISH_ENABLED` (`true` só após a aprovação).
- **Somente leitura nos CRMs.** O script faz apenas requisições HTTP `GET`. Tokens do Kommo e webhooks do Bitrix não existem
  em versão "só leitura"; por isso a garantia está no código. No Bitrix, o ideal é o webhook pertencer a um usuário dedicado
  com permissão apenas de visualização do Calendário da Empresa.
- O log do Actions é público: o script imprime somente `GATES: PASS/FAIL` e contagens de verificação, nunca URLs, tokens nem dados pessoais.

## Fluxo
1. `workflow_dispatch` (manual) → `mode=check`: roda os testes sintéticos, lê Kommo e Bitrix (GET), valida e **não grava nada**.
2. `mode=publish` (exige `PUBLISH_ENABLED=true`): grava `data/data.json` e `data/status.json` e faz commit com o `GITHUB_TOKEN`.
3. Rotina diária: linha `schedule` comentada no workflow (`0 14 * * 1-6` = 11h de Brasília, seg–sáb). Ativar só após uma execução real validada.

## Regras de negócio
- **Filial (Bitrix)** = cor do evento (azul = Botafogo, vermelho = Barra, laranja/amarelo = Goiânia; `#FCAA1C` provisoriamente Goiânia).
  **Filial (Kommo)** = funil da SDR (Fabiana, Jordana, Luana = Botafogo; Joyce = Barra; Waleska = Goiânia).
- **Indicação** é número à parte: fora de "Leads V4", de "Em atendimento" e das taxas de leads.
- **Fora do escopo**: eventos de closers, social selling, tag SS e criador não identificado.
- **Status**: faltou (não responde, não atende, imprevisto, enrolado no trabalho, tentar vir até o final do dia); realizada
  (negociando, buscando forma de pagamento, TRAT); vetado só conta como comparecimento quando confirmado.
- **Taxas**: comparecimento = (realizada + fechou + não fechou + vetado) ÷ elegíveis; no-show = faltou ÷ elegíveis;
  elegíveis = previstos − desmarcou − remarcado (confirmada e pendente ficam no denominador).
- **Kommo**: classificação pelo **ID da etapa** (`config/kommo_map.json`), não pelo nome.
- O no-show da agenda é o do Bitrix (não se soma à etapa "No Show" do Kommo).

## Falha e histórico
- Verificações (gates): totais fecham, sem IDs duplicados, toda unidade identificada, queda de leads > 10% bloqueia.
- Se algo falhar: `data/data.json` **não é alterado** (mantém a última versão válida), `status.json` registra o erro e o painel mostra um aviso.
- A base histórica aprovada fica congelada em `data/history_frozen.json` (agregados) e não é reescrita; o Bitrix é recalculado numa janela
  móvel de 14 dias a partir do fim do histórico.

## Testes
`cd dashboard-executivo-leads-agenda && python -m unittest discover -s tests` (somente dados sintéticos).
