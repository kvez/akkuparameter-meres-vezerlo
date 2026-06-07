FG / FIAMM AGM VRLA AKKU PARAMÉTEREK VALIDÁLÁSA – REVIEW PONTOK
=================================================================

Források:
- FIAMM Engineering Manual – AGM Technology, Edition 02/2026 Rev.15
- FIAMM FG Folder, Rev. 11.2025
- FIAMM FGH / FGHL Folder, Rev. 11.2025
- FIAMM VRLA Lead Acid Stationary Batteries Product Information Sheet, Rev. 12, 2023-10-10

Cél:
A programban használt FIAMM FG AGM VRLA akkumulátor-paraméterek ellenõrzése,
különös tekintettel a kapacitásra, töltésre, kisütési határokra, hõmérséklet-
kompenzációra, belsõ ellenállásra, biztonságra és élettartamra.


1. AKKUTÍPUS ÉS KAPACITÁS-PARAMÉTEREK
--------------------------------------

Review pontok:

[ ] A programban szereplõ akkutípus valóban FIAMM FG, nem FGH, FGHL, FGL vagy más sorozat.

[ ] Az FG általános célú AGM VRLA akku, nem kifejezetten high-rate UPS típus.
    Emiatt a nagyáramú kisütési / rövid idejû terhelési logikát óvatosan kell kezelni.

[ ] A konkrét típus pontosan azonosított:
    - FG20121
    - FG20721
    - FG21201
    - stb.

[ ] A programban használt kapacitásérték nem keveri a C10 és C20 adatokat.

[ ] A C10 adat értelmezése:
    - 10 órás kisütés
    - 1.80 V/cell végfeszültségig
    - gyártói engineering manual szerint 20 °C referencia
    - FG folder táblázatban 25 °C feltétel is szerepel

[ ] A C20 adat értelmezése:
    - 20 órás kisütés
    - 1.75 V/cell végfeszültségig
    - FG folder szerint 25 °C

[ ] Töltõáram, boost áramlimit, kisütési teszt és SOC számítás alapja lehetõleg C10 legyen,
    ne C20.

[ ] Példa FG20121 esetén:
    - névleges feszültség: 12 V
    - C10 kapacitás: 1.09 Ah
    - C20 kapacitás: 1.2 Ah
    - ezért a programban a C10 alapú számításokhoz 1.09 Ah-t kell használni, nem 1.2 Ah-t.

[ ] Ha a programban csak egy „capacity_Ah” mezõ van, egyértelmûen dokumentálni kell,
    hogy az C10 vagy C20 érték.

[ ] Ha több kapacitásérték szerepel, javasolt külön mezõk:
    - capacity_C10_Ah
    - capacity_C20_Ah
    - capacity_reference_temperature_C
    - capacity_end_voltage_V_per_cell


2. CELLASZÁM ÉS FESZÜLTSÉGSZINTEK
---------------------------------

Review pontok:

[ ] A cellaszám helyesen van megadva:
    - 6 V-os FG akku: 3 cella
    - 12 V-os FG akku: 6 cella

[ ] Minden cellafeszültségbõl számolt határérték a megfelelõ cellaszámmal van szorozva.

[ ] 12 V-os FG akku esetén alapértékek:
    - float 20 °C-on: 2.27 V/cell x 6 = 13.62 V
    - boost 20 °C-on: 2.40 V/cell x 6 = 14.40 V
    - C10 kapacitásteszt végfeszültség: 1.80 V/cell x 6 = 10.80 V
    - C20 adatlap szerinti végfeszültség: 1.75 V/cell x 6 = 10.50 V

[ ] 6 V-os FG akku esetén alapértékek:
    - float 20 °C-on: 2.27 V/cell x 3 = 6.81 V
    - boost 20 °C-on: 2.40 V/cell x 3 = 7.20 V
    - C10 kapacitásteszt végfeszültség: 1.80 V/cell x 3 = 5.40 V
    - C20 adatlap szerinti végfeszültség: 1.75 V/cell x 3 = 5.25 V

[ ] A programban ne keveredjenek az alábbi fogalmak:
    - névleges feszültség: 6 V / 12 V
    - nyugalmi feszültség / OCV
    - float töltési feszültség
    - boost töltési feszültség
    - terhelés alatti feszültség
    - kisütési végfeszültség
    - mélykisütési tiltási határ

[ ] A low-battery határ ne legyen automatikusan azonos a C10 kapacitásteszt végfeszültségével.
    A 1.80 V/cell már tesztfeltételhez tartozó mélyebb kisütési végpont, nem feltétlenül
    jó üzemi lekapcsolási határ.


3. FLOAT TÖLTÉS VALIDÁLÁSA
--------------------------

Gyártói irányelv:
- ajánlott float feszültség: 2.27 V/cell 20 °C-on
- hõmérséklet-kompenzáció: -2.5 mV/cell/°C
- tipikus float áram teljesen feltöltött AGM akkunál: kb. 0.3 mA/Ah
- a float áram nem megbízható SOC indikátor

Review pontok:

[ ] A program 20 °C-on 12 V-os FG akkunál kb. 13.62 V float célértéket használ.

[ ] A program 20 °C-on 6 V-os FG akkunál kb. 6.81 V float célértéket használ.

[ ] Van hõmérséklet-kompenzáció:
    - 12 V-os akku: -15 mV/°C blokk szinten
    - 6 V-os akku: -7.5 mV/°C blokk szinten

[ ] A kompenzáció képlete 12 V-os akkunál:
    float_voltage = 13.62 V + (20 °C - T_batt) x 0.015 V

[ ] A kompenzáció képlete 6 V-os akkunál:
    float_voltage = 6.81 V + (20 °C - T_batt) x 0.0075 V

[ ] Ha nincs közvetlen akkuhõmérséklet-mérés, a program ezt kezelje bizonytalanságként.

[ ] Magas hõmérsékleten ne maradjon túl magas float feszültség.

[ ] A program figyelje a tartós túlfeszültséget.

[ ] A program figyelje a szokatlanul magas float áramot, de ne használja önmagában SOC becslésre.

[ ] Legyen külön állapot:
    - float OK
    - float undervoltage
    - float overvoltage
    - float current abnormal
    - temperature compensation unavailable


4. BOOST / RECHARGE PARAMÉTEREK
-------------------------------

Gyártói irányelv:
- boost töltés: 2.40 V/cell 20 °C-on
- max. áramlimit: 0.25 C10
- boost leállítás, ha az áram 0.03 C10 alá esik
- boost leállítás, ha az akku hõmérséklete több mint 10 °C-kal a környezet fölé emelkedik
- hõmérséklet-kompenzáció a float kritérium szerint

Review pontok:

[ ] 12 V-os FG akkunál a boost célfeszültség 20 °C-on kb. 14.40 V.

[ ] 6 V-os FG akkunál a boost célfeszültség 20 °C-on kb. 7.20 V.

[ ] Boost áramlimit:
    I_boost_max = 0.25 x C10

[ ] Boost stop áram:
    I_boost_stop = 0.03 x C10

[ ] Példa FG20121 esetén, C10 = 1.09 Ah:
    - I_boost_max = 0.25 x 1.09 A = kb. 0.27 A
    - I_boost_stop = 0.03 x 1.09 A = kb. 33 mA

[ ] A boost módnak legyen maximális idõkorlátja.

[ ] A boost mód ne maradjon aktív szenzorhiba, öreg akku vagy hibás akku esetén.

[ ] Mélykisütött akkunál ne induljon azonnal teljes boost áram validáció nélkül.

[ ] Boost csak indokolt esetben fusson:
    - kisütés után
    - idõszakos karbantartásként
    - szerviz / teszt módban

[ ] Normál standby üzemben az alapállapot float legyen.

[ ] Boost közben figyelni kell:
    - akku feszültség
    - töltõáram
    - akkuhõmérséklet
    - környezeti hõmérséklet
    - dT/dt, ha rendelkezésre áll
    - túlfeszültség
    - túláram
    - no battery / szakadás


5. KISÜTÉSI HATÁROK ÉS SOC BECSLÉS
----------------------------------

Gyártói irányelv:
- az OCV-SOC csak közelítõ információ
- nyugalmi feszültséget csak akkor érdemes SOC-ra használni, ha az akku legalább 24 órája
  le van választva a töltõrendszerrõl

Review pontok:

[ ] Terhelés alatti feszültségbõl a program ne számoljon közvetlen SOC-t kompenzáció nélkül.

[ ] Töltés alatti feszültségbõl a program ne számoljon közvetlen SOC-t.

[ ] OCV-alapú SOC csak pihent akkunál legyen érvényes.

[ ] A program különböztesse meg:
    - charging voltage
    - loaded voltage
    - resting voltage / OCV
    - recovery voltage

[ ] A C10 kapacitásteszt végpontja 1.80 V/cell.

[ ] Ez 12 V-os akkunál 10.80 V, de ez nem feltétlenül ajánlott üzemi lekapcsolási határ.

[ ] Legyen több fokozat:
    - elõriasztás
    - low battery
    - load shed / részleges lekapcsolás
    - akkuterhelés tiltása
    - mélykisütés lockout

[ ] A program kezelje a feszültség-visszaemelkedést terhelés levétele után.

[ ] A program ne minõsítsen jó akkunak egy olyan akkut, amely terhelés nélkül jó feszültséget mutat,
    de kis terhelésre gyorsan összeesik.

[ ] SOC becslésnél érdemes kombinálni:
    - nyugalmi feszültség
    - coulomb counting, ha van
    - belsõ ellenállás trend
    - terhelés alatti feszültségesés
    - hõmérséklet
    - korábbi kapacitásteszt eredmény


6. HÕMÉRSÉKLET HATÁSA
---------------------

Gyártói irányelv:
- ajánlott üzemi hõmérséklet: kb. 10–30 °C
- AGM VRLA akkuk mûködhetnek kb. -20…+50 °C tartományban
- 20 °C felett az élettartam csökken
- alacsony hõmérsékleten a rendelkezésre álló kapacitás csökken

Review pontok:

[ ] A program kezelje külön:
    - hideg miatti kapacitáscsökkenés
    - meleg miatti élettartam-csökkenés
    - töltési feszültség hõkompenzáció
    - túlmelegedési védelem

[ ] Alacsony hõmérsékleten ne várjon névleges kapacitást.

[ ] Magas hõmérsékleten korlátozza vagy tiltsa a boost töltést.

[ ] Magas hõmérsékleten legyen float overtemperature figyelmeztetés.

[ ] Ha az akku elektronikával közös dobozban van, a környezeti hõmérséklet nem biztos,
    hogy azonos az akku hõmérsékletével.

[ ] Zárt fém dobozban az akku saját melegedése, a töltõ hõje és a környezeti hõ együtt
    értékelendõ.

[ ] Hõmérsékleti határértékeket külön kell kezelni:
    - figyelmeztetés
    - töltéskorlátozás
    - boost tiltás
    - teljes akkuleválasztás / hibastátusz

[ ] Ha nincs akkuhõmérséklet-szenzor, ezt a validációban kockázatként kell jelölni.


7. BELSÕ ELLENÁLLÁS / IMPEDANCIA VALIDÁLÁSA
-------------------------------------------

Gyártói irányelv:
- a belsõ ellenállás és rövidzárási áram 100% SOC és 20 °C mellett értendõ
- az értékek IEC 60896-21/22 szerint vannak meghatározva
- különbözõ mérõmûszerek eltérõ eredményt adhatnak

Review pontok:

[ ] A program ne tekintse abszolút igazságnak az impedanciaértéket.

[ ] Belsõ ellenállást inkább trendként használjon:
    - új akku baseline
    - öregedés miatti növekedés
    - kontakthiba gyanú
    - cellahiba gyanú

[ ] A mérés azonos körülmények között történjen:
    - hasonló SOC
    - hasonló hõmérséklet
    - azonos mérõáram
    - azonos mérési idõablak
    - azonos kábelezés

[ ] A kábel-, relé-, csatlakozó- és panel-ellenállás legyen külön kalibrálva.

[ ] Kis FG akkuknál a Faston kontaktus hibája jelentõs mérési hibát okozhat.

[ ] Ha a program Rint mérést végez, külön kell menteni:
    - nyers mért érték
    - kalibrált érték
    - kompenzációs érték
    - mérési áram
    - mérési hõmérséklet
    - mérési SOC / feszültségállapot

[ ] Ne legyen elfogadó döntés csak egyetlen Rint mérés alapján.

[ ] Javasolt több mérés átlaga, outlier szûréssel.


8. RIPPLE ÉS TÖLTÕRENDSZER
--------------------------

Gyártói irányelv:
- a töltõ ripple rontja az élettartamot
- növelheti a vízvesztést, hõmérsékletet és korróziót
- a feszültségszabályozás akkumulátor nélkül, stabil állapotban, terheléssel legyen jobb mint +/-1%
- a csúcs-csúcs ripple maradjon a float feszültség kb. 2.5%-án belül
- float üzemben az áramripple nem fordíthatja kisütési irányba az akkuáramot

Review pontok:

[ ] A töltõ DC pontossága mérve van akku nélkül és akkuval is.

[ ] Terhelésváltásnál nincs túllövés a float / boost maximum fölé.

[ ] Float üzemben az akkun nem folyik periodikus kisütési irányú ripple áram.

[ ] 2 s-os firmware mintavétellel a gyors ripple nem detektálható.

[ ] Ripple validációhoz külön mérés kell:
    - oszcilloszkóp
    - megfelelõ földelés
    - megfelelõ sávszélesség
    - megfelelõ trigger
    - akkuval és akku nélkül is

[ ] A program kezelje:
    - charger overvoltage
    - charger undervoltage
    - charger unstable
    - reverse current
    - no battery
    - battery disconnected


9. PÁRHUZAMOS AKKUK / TÖBB BLOKK
--------------------------------

Gyártói irányelv:
- azonos stringen belül azonos típus, modell, gyártási dátum és darabszám ajánlott
- szimmetrikus kábelezés szükséges
- általában kb. 4 párhuzamos string még szokásosnak tekinthetõ

Review pontok:

[ ] A program ne feltételezze automatikusan, hogy a párhuzamos akkuk egyformán osztoznak az áramon.

[ ] Új és régi akkuk ne legyenek keverve.

[ ] FG, FGH és FGHL típusok ne legyenek keverve ugyanabban a stringben.

[ ] Ha több akku van, lehetõség szerint külön mérés kell:
    - egyedi blokkfeszültség
    - egyedi hõmérséklet
    - egyedi belsõ ellenállás

[ ] A kábelezés legyen szimmetrikus.

[ ] A biztosítékok és vezetékek minden stringre megfelelõek legyenek.

[ ] Egy gyenge akku ne tudja lerontani az egész párhuzamos rendszert észrevétlenül.


10. MECHANIKAI ÉS BEKÖTÉSI VALIDÁLÁS
------------------------------------

Gyártói információ:
- FG akkuk álló és fekvõ helyzetben is telepíthetõk
- kisebb típusok Faston csatlakozósak
- telepítéskor kerülni kell az ütést, terminálnál emelést, rövidzárat
- a blokkok között hõleadási távolság ajánlott

Review pontok:

[ ] A Faston / Flag / M5 csatlakozó megfelel a várható maximális áramnak.

[ ] A csatlakozó típusa egyezik a konkrét akkuval.

[ ] Van biztosíték közvetlenül az akku közelében.

[ ] Van fordított polaritás elleni védelem vagy legalább detektálás.

[ ] Van szakadt akku / rossz kontaktus detektálás.

[ ] A mechanikai rögzítés bírja a várható rezgést.

[ ] A kábelek nem terhelik mechanikailag a Faston csatlakozót.

[ ] A dobozban van hõleadási hely.

[ ] A fém dobozban nincs akku pólushoz közeli zárlatveszély.

[ ] A program detektálni tudja:
    - fordított polaritás
    - no battery
    - szakadt akku
    - gyenge kontaktus
    - túl nagy belsõ ellenállás
    - túl gyors feszültségesés terheléskor


11. TÁROLÁS, STANDBY ÉS MÉLYKISÜTÉS
-----------------------------------

Gyártói biztonsági információ:
- a töltött ólomakkukat fedett, hûvös helyen kell tárolni
- rövidzár ellen védeni kell
- a tárolási és kezelési utasításokat be kell tartani

Review pontok:

[ ] A rendszer hosszú áramtalanított állapotban nem meríti le az akkut.

[ ] A nyugalmi fogyasztás validált:
    - MCU sleep
    - mérõosztók
    - relék
    - LED-ek
    - kommunikációs modulok
    - védelmi áramkörök

[ ] Van mélykisütés elleni leválasztás.

[ ] Mélykisütés után a program nem engedi azonnal normál üzembe az akkut validáció nélkül.

[ ] A program tárolja vagy jelzi:
    - utolsó teljes feltöltés ideje
    - utolsó kisütés ideje
    - minimum mért feszültség
    - mélykisütési események száma
    - becsült akkukor / ciklusszám

[ ] Hosszú tárolás után legyen újratöltési / ellenõrzési folyamat.

[ ] A rendszerben minden külsõ fogyasztó leválasztható legyen mélykisütés elõtt.


12. BIZTONSÁGI REVIEW
---------------------

Gyártói biztonsági információ:
- VRLA akku normál üzemben zárt rendszer, de savat, ólmot és jelentõs energiát tartalmaz
- töltéskor hidrogén és oxigén keletkezhet
- rövidzár esetén nagy áram és elektromos veszély léphet fel
- sérült akku esetén savas elektrolit kerülhet ki

Review pontok:

[ ] Biztosíték kötelezõ az akkuágban.

[ ] A töltõ hibája nem okozhat korlátlan túltöltést.

[ ] A terhelés hibája nem okozhat korlátlan kisütést vagy zárlatot.

[ ] Overvoltage esetén a program kapcsolja le a töltést.

[ ] Overtemperature esetén a program korlátozza vagy tiltja a töltést.

[ ] Mélykisütött akkut a rendszer ne kezeljen automatikusan egészségesként.

[ ] Zárt doboz esetén legyen valamilyen szellõzés; teljesen hermetikusan zárt doboz kerülendõ.

[ ] Az akku közelében ne legyen szikraképzõ kontaktus vagy nem védett relé, ha gázképzõdés kockázata fennáll.

[ ] A program hibastátuszai legyenek egyértelmûek:
    - battery missing
    - battery reversed
    - battery deeply discharged
    - battery weak
    - battery internal resistance high
    - charger fault
    - overtemperature
    - overvoltage
    - undervoltage


13. ÉLETTARTAM ÉS CSEREKRITÉRIUM
--------------------------------

Gyártói irányelv:
- az akku élettartamának vége jellemzõen akkor tekinthetõ elértnek,
  ha már kevesebb mint a névleges kapacitás 80%-át képes leadni
- FG sorozat: kb. 5 év design life
- magas hõmérséklet jelentõsen csökkenti az élettartamot

Review pontok:

[ ] A program ne csak pillanatnyi feszültség alapján minõsítse az akkut.

[ ] Legyen kapacitás- vagy terheléses funkcionális teszt.

[ ] A 80% alatti kapacitást a rendszer kezelje csereérett állapotként.

[ ] A belsõ ellenállás növekedése legyen trendelve.

[ ] A magas hõmérsékletû üzemidõt érdemes naplózni.

[ ] A mélykisütések számát érdemes naplózni.

[ ] A boost ciklusok és töltési rendellenességek száma érdemes naplózásra.

[ ] Akkucsere után baseline mérés szükséges:
    - OCV
    - terhelt feszültség
    - belsõ ellenállás
    - float áram
    - töltési viselkedés


14. KONKRÉT PROGRAM-PARAMÉTEREK JAVASOLT STRUKTÚRÁJA
----------------------------------------------------

Javasolt konfigurációs mezõk:

battery_model = "FG20121"
battery_series = "FG"
battery_nominal_voltage_V = 12.0
battery_cell_count = 6
capacity_C10_Ah = 1.09
capacity_C20_Ah = 1.20
capacity_reference_temperature_C = 20 vagy 25, forrástól függõen dokumentálva
float_voltage_V_per_cell_20C = 2.27
boost_voltage_V_per_cell_20C = 2.40
temperature_compensation_V_per_cell_C = -0.0025
boost_current_limit_C = 0.25
boost_stop_current_C = 0.03
capacity_test_end_voltage_V_per_cell_C10 = 1.80
capacity_test_end_voltage_V_per_cell_C20 = 1.75
ocv_soc_rest_time_h = 24
end_of_life_capacity_ratio = 0.80

Számított értékek 12 V / 6 cella esetén:

float_voltage_20C = 13.62 V
boost_voltage_20C = 14.40 V
float_temp_compensation = -15 mV/°C
boost_current_limit_A = 0.25 x C10
boost_stop_current_A = 0.03 x C10
C10_test_end_voltage = 10.80 V
C20_test_end_voltage = 10.50 V


15. MINIMÁLIS VALIDÁCIÓS CHECKLIST
----------------------------------

[ ] Pontos FG típus azonosítva.
[ ] 6 V / 12 V és cellaszám helyes.
[ ] C10 és C20 nincs összekeverve.
[ ] Töltõáram C10 alapján számolva.
[ ] Float 2.27 V/cell @ 20 °C.
[ ] Boost 2.40 V/cell @ 20 °C.
[ ] Hõkompenzáció -2.5 mV/cell/°C.
[ ] Boost áramlimit max. 0.25 C10.
[ ] Boost stop 0.03 C10 vagy túlmelegedés.
[ ] OCV-SOC csak legalább 24 h pihent akkunál.
[ ] Terhelés alatti feszültség nem közvetlen SOC.
[ ] Belsõ ellenállás kalibrált és trendként kezelt.
[ ] Kábel / relé / panel ellenállás külön kompenzált.
[ ] Ripple külön mérve, nem 2 s-os firmware ciklussal.
[ ] No battery, reverse battery, deep discharge detektálva.
[ ] Overvoltage / undervoltage / overtemperature védelem van.
[ ] Akkuág biztosítékkal védett.
[ ] Mechanikai rögzítés, Faston/Flag terhelhetõség ellenõrizve.
[ ] Zárt doboz szellõzése átgondolva.
[ ] 80% alatti kapacitás csereérett állapotként kezelve.
[ ] Akkucsere után baseline mérés készül.


16. LEGFONTOSABB REVIEW MEGÁLLAPÍTÁSOK
--------------------------------------

1. FG akkunál a programban ne C20 adatból számolj töltõáramot, boost limitet és
   kisütési validációt, hanem C10-bõl.

2. FG20121 esetén a C10 érték 1.09 Ah, míg a C20 érték 1.2 Ah.
   Ez kis akkunál már érdemi különbséget okoz a töltõáram- és stopáram-számításban.

3. 12 V-os FG akkunál a helyes 20 °C-os fõ töltési pontok:
   - float: 13.62 V
   - boost: 14.40 V

4. A hõmérséklet-kompenzáció 12 V-os akkunál -15 mV/°C.
   Enélkül meleg környezetben túltöltés, hidegben alultöltés lehetséges.

5. Az OCV alapú SOC csak pihent, töltõrõl leválasztott akkunál használható.
   Üzemi rendszerben önmagában nem elég.

6. A belsõ ellenállást nem abszolút gyártói értékként, hanem saját rendszerben kalibrált
   trendként érdemes kezelni.

7. A Faston csatlakozó, kábel, relé és panel ellenállása kis FG akkuknál jelentõs hibát
   vihet a mérésbe.

8. A 2 másodperces firmware ciklus alkalmas lassú állapotfelügyeletre, de nem alkalmas
   töltõ-ripple vagy gyors tranziensek validálására.

9. Mélykisütés, túltöltés és magas hõmérséklet ellen külön firmware- és lehetõleg
   hardveres védelem is szükséges.

10. Élettartam szempontból a 80% alatti tényleges kapacitás már csereérett állapotnak
    tekintendõ.