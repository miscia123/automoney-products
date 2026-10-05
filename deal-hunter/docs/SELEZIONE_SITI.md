# Selezione dei siti: cosa tiene il bot e perché

Base: il documento "Siti di aste per fare affari" (circa 95 siti in 18 sezioni). Criteri:

1. **Oggetti preziosi o da collezione**, non immobili, aziende o macchinari.
2. **Accessibile a un privato in Italia**, senza partita IVA.
3. **Affari possibili**: venditori con fretta, poca concorrenza, prezzi base bassi.
4. **Automatizzabile**: un'API o pagine leggibili senza login, con anti-bot superabile.
5. **Prezzo verificabile**: esistono dati di venduto o un valore oggettivo (il metallo).

**Come sono stati verificati (5 ottobre 2026).** Dall'ambiente cloud dove è stato scritto il codice i siti non erano raggiungibili: la rete era bloccata. Ogni endpoint è stato quindi controllato sul codice di scraper open source aggiornati al 2025-2026 e con ricerche web. I parser sono coperti da test su campioni di risposta in quel formato. Il comando `dealhunter doctor`, lanciato dalla tua rete, dice in 2 minuti quali sorgenti rispondono davvero.

## A. Nel bot, attive di default

| Sorgente | Cosa trova | Accesso tecnico | Frequenza | Note |
|---|---|---|---|---|
| **Subito.it** | Privati con fretta: oro, orologi, monete | API JSON `hades.subito.it`, header `x-subito-channel: web` | 10 min | La fonte numero uno per gli affari "veri". Rischio truffe alto: il bot lo pesa. |
| **eBay.it / .de** | Aste in chiusura, annunci nuovi | Browse API ufficiale (chiavi gratuite) o pagina di ricerca | 15 min | Serve anche per i **prezzi venduti**. |
| **Vinted** | Gioielli e orologi sottoprezzati | Nuova API `api.vinted.it/svc-catalogue` (quella vecchia è stata chiusa a settembre 2026) | 15 min | Usa DataDome: funziona da casa, spesso non da un server. |
| **Wallapop** | Privati, inventario italiano più piccolo | API `api.wallapop.com/api/v3/search`, nessun anti-bot | 20 min | |
| **Catawiki** | Aste settimanali di orologi, gioielli, monete, carte | Dati `__NEXT_DATA__` della ricerca + API delle offerte `/buyer/api/v3/bidding/lots` | 30 min | PerimeterX; commissione 9% + 3 €. Avvisa solo nelle ultime 24 ore. |
| **Affide** | Aste su pegno: oro a peso, orologi, monete | Pagine HTML (TYPO3) calendario → asta → lotto | 3 ore | Lotti periziati: rischio falso minimo. **Diritti d'asta 25% + IVA** (circa 30,5%). |
| **Zoll-Auktion** (DE) | Aste dello Stato tedesco: orologi, gioielli, metalli, monete | HTML senza protezioni; categorie 243, 242, 1115, 240 | 1 ora | Nessuna commissione. Spedisce all'estero **solo se l'asta lo prevede**: il bot segnala i lotti "solo ritiro". |
| **Vendite giudiziarie** | Gioielli, Rolex e preziosi da fallimenti e pignoramenti | **PVP**: API JSON pubblica (lotti MOBILI). **Astegiudiziarie.it**: API `webapi.astegiudiziarie.it` (tipologia 11, arte e oreficeria). **Fallcoaste**: HTML delle categorie orologi-gioielli e preziosi | 6 ore | Nessuna garanzia (art. 2922 c.c.); diritti IVG intorno al 15% + IVA. Offerta minima = 75% della base. |

## B. Nel bot, spente di default

| Sorgente | Perché è spenta | Come accenderla |
|---|---|---|
| **Buyee** (Yahoo Auctions Japan) | Risponde solo a un browser vero (challenge HTTP 202). Yahoo blocca gli IP europei dal 2022, quindi il proxy è obbligatorio. | `pip install playwright`, poi `enabled: true`. Utile per orologi giapponesi e carte. Il bot calcola IVA 22%, dazi e sdoganamento. |
| **LiveAuctioneers** | Case d'asta USA: commissioni del 20-30% e importazione | Già usato per i **prezzi venduti** (archivio con i prezzi di aggiudicazione). |

## C. Fonti dei prezzi venduti (verifica di ogni prezzo)

| Fonte | Per cosa | Accesso |
|---|---|---|
| eBay.it / eBay.de **venduti** | Tutto | Pagina con `LH_Sold=1&LH_Complete=1`. La Finding API è stata spenta a febbraio 2025. |
| Archivio LiveAuctioneers | Orologi, gioielli, monete, arte, borse | API `search-party-prod`, `status: archive`, campo `salePrice` |
| Cardmarket Price Guide | Carte (Pokémon, Magic, Yu-Gi-Oh, One Piece, Lorcana) | File JSON pubblici giornalieri, medie del venduto a 1, 7 e 30 giorni |
| Spot oro e argento | Oro a peso, sterline, marenghi, lingotti | goldprice.org, con gold-api.com come riserva |
| Cambi BCE | JPY, USD, GBP, CHF | XML giornaliero BCE |
| Risultati Affide | Oro e preziosi su pegno | Il "prezzo realizzato" è pubblicato sul lotto e il bot lo legge già |

## D. Da aggiungere in seguito (utili, ma serve un browser o lavoro manuale)

- **ProntoPegno / KrusoK**: è un'app a pagina unica (`fe.prontopegno.it`). Bisogna catturarne le chiamate dal browser.
- **Gobid**: ha aste di gioielli e orologi, ma è dietro Cloudflare, quindi serve Playwright.
- **IVG / astagiudiziaria.com, Astemobili, Bidinside "lotti invenduti"**: HTML semplice, sono il prossimo connettore.
- **Chrono24**: prezzi richiesti degli orologi come riferimento. È dietro Cloudflare, quindi serve Playwright.
- **NumisBids, Sixbid**: aste numismatiche, dietro Cloudflare.
- **Enchères du Domaine**: le vendite di Stato francesi hanno un WAF severo e captcha, e ce ne sono poche all'anno. Meglio l'alert email del sito.
- **Invaluable, the-saleroom, Barnebys, Auctionet, Dorotheum, Drouot**: altri aggregatori di case d'asta. LiveAuctioneers copre già lo stesso tipo di dato.
- **Case d'asta italiane** (Cambi, Pandolfini, Wannenes, Bolaffi, Il Ponte, Finarte, Bertolami, Nomisma, NAC): molte passano da Bidinside, da cui si leggono gli invenduti.
- **Goldin, Heritage, Fanatics Collect, GreatCollections, The Coin Cabinet**: Stati Uniti e Regno Unito, con importazione. Utili più come prezzi venduti che come fonte di acquisto.

## E. Scartati per questo bot

| Siti | Motivo |
|---|---|
| Immobili: PVP e portali immobiliari, Demanio, Avvisi Notarili/RAN, ISMEA, NPL Immobiliare, doValue, Prelios, Intrum, Allsop, Savills, iamsold, Auction House UK, LED Thailandia, Emirates | Non sono oggetti. Richiedono perizie, mutui e due diligence legale: non si comprano e rivendono con un alert. |
| Macchinari: Troostwijk, Surplex, Vavato, Klaravik, BVA, Industrial Discount | B2B, non preziosi, ritiro sul posto |
| Stock e resi: Merkandi, B-Stock | B2B, pallet al buio |
| Business e domini: Empire Flippers, Flippa, Acquire, broker, GoDaddy, DropCatch, NameJet, Sedo, ExpiredDomains | Altra classe di investimento |
| Auto B2B: Autorola, Spoticar Trade, Manheim | Solo commercianti con partita IVA |
| Auto (Astauto, Collecting Cars, BaT, Car & Classic, RM Sotheby's…) | Fuori dal perimetro "preziosi e collezionismo". Si possono aggiungere con le stesse regole. |
| Bidoo e aste al centesimo | Trappola: si paga ogni rilancio |
| Whatnot, eBay Live (box break) | Aste dal vivo in streaming, più vicine al gioco d'azzardo |
| Facebook Marketplace | Richiede login e vieta l'automazione: controllalo a mano |
| Watchfinder, Chronext, WatchBox, Bob's, SwissWatchExpo, Bucherer CPO, The RealReal, Fashionphile, Rebag | Rivenditori: prezzi al dettaglio, cioè il tuo prezzo di **uscita**, non di acquisto |
| Phillips, Christie's, Sotheby's, Antiquorum, Monaco Legend | Fascia alta, concorrenza mondiale: affari rari |
| EveryWatch, WatchCharts | A pagamento (WatchCharts API da circa 600 $/mese) |
| ADM aste online | Poche vendite, e serve SPID. I grandi lotti di orologi passano dagli IVG e da Fallcoaste, che sono già coperti. |
| ANBSC, Difesa, Douane, Cessions immobilières, BOE Subastas | Immobili, aziende, lotti grandi o altri paesi senza preziosi regolari |
| Gelardini & Romani, iDealwine | Vino: margini bassi e rischio di conservazione. È tra le categorie da aggiungere più avanti. |
| Depop | Streetwear di basso valore |
