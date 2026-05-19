# Egendefinerte steder

Bilder tatt med mobiltelefon har vanligvis GPS-data som viser hvor det ble tatt.
Bildebank bruker et system kalt [H3](https://h3geo.org) som deler verden i et
nett av heksagoner (sekskanter). Hvert heksagon kan underdeles i 7 mindre, i 
nivåer fra 0 som er 4,34 km², til nivå 10 som er 0,1 km².

Bildebank bruker kommandoen [`bildebank geo-scan`](/help/geo-scan.md) til å finne
hvilket heksagon i hver størrelse fra 0 til 10 som bildet er tatt i. Ved
å definere ett eller flere heksagon av valgfri størrelse, så kan man definere
steder, som "Narvik", "Middelhavet" eller "Fjelltopper på Hadseløya" og se
bildene tatt på disse stedene.

På nettsiden [h3geo.org/](https://h3geo.org/)
kan man zoome inn til man får heksagoner av passende størrelse, og markere
det som er et område man vil finne bilder tatt i. Kodene for disse limes inn
i boksene på siden for [egendefinerte steder](/geo/custom-places).

Spør Tom hvis det er vanskelig, så lager han stedene for deg eller viser
deg hvordan det gjøres.
