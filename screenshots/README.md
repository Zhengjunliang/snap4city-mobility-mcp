# Screenshots

Sottocartella con le immagini degli snapshot delle schermate (istruzione di consegna, punto "screenshots").
Le immagini vanno catturate eseguendo l'advisor sulla **Snap4City JupyterHub** con la dashboard attiva
(vedi `README.md` §3), poi salvate qui con i nomi elencati sotto. La relazione le richiama da
`relazione/figures/`.

## Immagini da catturare

| File | Scenario | Query di esempio | Cosa deve mostrare |
|---|---|---|---|
| `01-tre-modi.png` | Tre modi (default) | *"da Piazza del Duomo a Santa Croce"* | le tre rotte (a piedi / auto / trasporto pubblico) disegnate insieme sulla mappa + risposta di confronto |
| `02-modo-singolo.png` | Modo singolo | *"da Piazza del Duomo a Santa Croce a piedi"* | una sola rotta (a piedi) sulla mappa |
| `03-gps-vicino.png` | Categoria vicino alla posizione (GPS) | *"portami alla farmacia più vicina"* | origine = posizione GPS, destinazione = servizio più vicino trovato |
| `04-servizi-lungo-percorso.png` | Servizi lungo il percorso | *"da A a B con le farmacie lungo il percorso"* | rotta + pin viola dei servizi campionati lungo il tragitto |
| `05-bus-legs.png` | Trasporto pubblico (leg walk/ride) | *"da A a B in autobus"* | geometria per-leg a due colori (tratti a piedi + tratto in bus con pin fermata) |
| `06-chips-dettaglio.png` | Selettore rotte + dettaglio | (un turno a tre modi, poi click su una chip) | dock delle chip fra chat e input + bolla di dettaglio della rotta scelta |
| `07-parcheggi.png` | Parcheggi (modo auto) | *"da A a B in auto"* | pin dei parcheggi vicino alla destinazione con posti liberi |

## Note

- Formato PNG, ritaglio sulla finestra della dashboard (chat box + widgetMap).
- I nomi file sono richiamati come figure nella relazione: mantenerli invariati o aggiornare
  di conseguenza i `\includegraphics` in `relazione/relazione.tex`.
- Gli screenshot 04–07 sono opzionali ma consigliati (mostrano le funzioni avanzate).
