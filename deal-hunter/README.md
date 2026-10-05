# DealHunter

Bot che controlla di continuo aste e marketplace (Subito, eBay, Vinted, Wallapop, Catawiki, Affide, Zoll-Auktion e le vendite giudiziarie) in cerca di affari su **oro, orologi, gioielli, monete, borse e carte**. Ogni prezzo viene **verificato sui venduti reali**, e quando trova un grande affare ti scrive su Telegram.

Per ogni annuncio calcola:

- **Valore di mercato**: mediana dei venduti comparabili su eBay.it, eBay.de, l'archivio LiveAuctioneers e Cardmarket, oppure il valore del metallo fino (peso × titolo × spot) per oro e monete.
- **Costo finale in mano**: prezzo, diritti d'asta, IVA sui diritti, spedizione, e per gli acquisti extra UE dazi, IVA all'import e sdoganamento.
- **Costo delle riparazioni** per gli oggetti da revisionare o non funzionanti, in base a marca e fascia.
- **Incasso netto alla rivendita** sul canale giusto: Chrono24, Catawiki, compro oro, Cardmarket o Vestiaire, al netto delle commissioni.
- **Guadagno e ROI**.
- **Rischio e affidabilità (0-100)** con le motivazioni: piattaforma, venditore, categoria spesso contraffatta, sconto "troppo bello per essere vero", poche informazioni, incertezza della stima.
- **Tempo di rivendita** stimato in giorni.
- **Offerta massima consigliata** per le aste: il prezzo oltre cui non c'è più il margine che vuoi.
- Facoltativo: **il parere di un perito AI** (Claude), solo sui candidati migliori. Guarda foto, descrizione e comparabili e segnala falsi, comparabili sbagliati e riparazioni.

La scelta dei siti, con le motivazioni, è in [docs/SELEZIONE_SITI.md](docs/SELEZIONE_SITI.md).

## Come funziona

```
 sorgenti (in parallelo, ciascuna col suo intervallo)
   Subito 10' · Vinted 15' · eBay 15' · Wallapop 20' · Catawiki 30'
   Zoll 1h · Affide 3h · PVP/Astegiudiziarie/Fallcoaste 6h
        │  solo annunci nuovi o con prezzo cambiato (SQLite)
        ▼
 estrazione a regole (marca, modello, referenza, grammi, carati, difetti, parole da falso)
        ▼
 comparabili venduti (eBay.it/.de, LiveAuctioneers, Cardmarket) con cache di 24 ore
 + valore del metallo (spot oro e argento, cambi BCE)
        ▼
 costo finale → riparazioni → rivendita → guadagno/ROI → rischio → tempi → offerta massima
        ▼
 livello: 🔥 grande affare / ✅ buono / 👀 da seguire
        ▼
 perito AI (facoltativo, solo i migliori) → Telegram / email + riepilogo serale
```

Perché è veloce:

- Le richieste partono in parallelo, con un limite per sito.
- I prezzi venduti restano in cache: cento annunci di "Rolex Datejust 16234" costano una sola ricerca.
- Valuta solo gli annunci nuovi o che hanno cambiato prezzo.
- L'analisi AI tocca solo pochi candidati per giro.

## Installazione (5 minuti)

**Dove farlo girare.** Il posto migliore è un computer sempre acceso **a casa**, in Italia: un Raspberry Pi, un mini PC o un NAS. Vinted, eBay e Catawiki bloccano spesso gli IP dei server in datacenter e quasi mai le connessioni di casa. Su una VPS serve un proxy residenziale italiano (variabile `DEALHUNTER_PROXY`).

```bash
cd deal-hunter
python3 -m venv .venv && . .venv/bin/activate
pip install .
cp config/config.example.yaml config/config.yaml
cp .env.example .env        # inserisci token Telegram e, se vuoi, la chiave Anthropic
set -a; . ./.env; set +a

dealhunter doctor           # prova ogni sito dalla tua rete e dice cosa funziona
dealhunter test-alert       # messaggio di prova su Telegram
dealhunter run --force      # un giro completo
dealhunter daemon           # sempre attivo, ogni sito al suo intervallo
dealhunter report           # i migliori affari delle ultime 24 ore
```

Con Docker: `docker compose up -d` (legge `.env` e `config/`, il database va in `data/`).

**Telegram.**
1. Scrivi a @BotFather, crea un bot e copia il token.
2. Manda un messaggio qualsiasi al tuo bot.
3. Apri `https://api.telegram.org/bot<TOKEN>/getUpdates` e copia `chat.id`.

**Buyee (Giappone).** Serve un browser vero: `pip install playwright && playwright install chromium`, poi `buyee: {enabled: true}`. Su un server senza schermo lancia il bot con `xvfb-run dealhunter daemon`.

## Personalizzare

- `config/watchlist.yaml`: cosa cercare, con prezzo minimo e massimo (scarta cinturini, scatole e repliche) e le traduzioni per Buyee.
- `config/config.yaml`: soglie degli alert (guadagno minimo, ROI, rischio massimo), intervalli, ore di silenzio, ora del riepilogo.
- `dealhunter/config.py`: commissioni di ogni piattaforma, canali di rivendita, dazi. Sono valori verificati a ottobre 2026; aggiornali se cambiano.

## Come leggere un alert

```
🔥 GRANDE AFFARE · catawiki · orologio
Omega Speedmaster Professional Moonwatch 3570.50
💶 Prezzo: 1.500 EUR (offerta attuale, 12 offerte, fine tra 10 minuti)
📈 Prezzo finale stimato: 2.230 EUR (i calcoli usano questo)
🧾 Costo finale in mano: € 2.458 (comm. € 204, sped. € 25)
📊 Valore di mercato: € 4.200 (range € 4.138–€ 4.300)
    ↳ mediana di 10 comparabili (10 venduti); affidabilità stima 81%
💰 Guadagno stimato: € 1.334 (ROI 54%) vendendo su Chrono24/Catawiki (privato)
⏱ Tempo di rivendita: 10–40 giorni
🛡 Affidabilità: 77/100 (rischio 23)
🎯 Offerta massima consigliata: 2.873 EUR
🔎 Venduti simili: € 4.100 · € 4.200 · € 4.300
```

È l'output reale del motore su un caso di prova (il test `test_pipeline_watch_auction_and_fake_skip`). La stessa asta a **3 ore** dalla fine **non** fa scattare l'alert: il prezzo finale previsto è 3.324 € e il guadagno scende a 141 €. Il bot non si fa ingannare dalle offerte basse di metà asta.

Per le aste il guadagno è calcolato su un **prezzo finale stimato**, non sull'offerta attuale: la concorrenza farà salire il prezzo. Il numero da usare è l'**offerta massima consigliata**.

## Limiti da conoscere

- **I siti cambiano.** Se una sorgente fallisce per 3 giri di fila arriva un avviso su Telegram, e `dealhunter doctor` mostra quale. I parser sono isolati, uno per file in `dealhunter/sources/`.
- **Verifica dal vivo.** Gli endpoint sono stati verificati su scraper open source del 2025-2026, ma non da una rete italiana. Il primo `dealhunter doctor` a casa tua è il collaudo vero. Affide e Zoll hanno parser "tolleranti", basati sul testo della pagina, perché la loro struttura HTML non era verificabile.
- **La stima non è una perizia.** Il bot ti dice dove guardare; prima di pagare verifica sempre di persona (pesa e saggia l'oro, controlla seriali e movimento).
- **Regole dei siti.** Molti siti vietano l'accesso automatico nelle condizioni d'uso. Il bot è prudente (pause, pochi accessi paralleli, solo dati pubblici), ma l'uso è sotto la tua responsabilità.
- **Fisco.** Comprare e rivendere con abitualità può diventare attività d'impresa, con partita IVA e regime del margine. Parlane con un commercialista.

## Test

```bash
pip install '.[dev]' && pytest
```

I test coprono l'estrazione degli attributi, i parser di ogni sorgente (su campioni di risposta), la valutazione, i costi d'importazione, l'offerta massima e la pipeline completa senza rete.
