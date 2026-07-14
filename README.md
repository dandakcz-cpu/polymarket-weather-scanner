# Polymarket Weather Scanner

Veřejná FastAPI stránka, která načítá aktivní Polymarket události, vyfiltruje trhy s nejvyšší denní teplotou a zobrazí živé ceny.

## Lokální spuštění

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Otevři `http://127.0.0.1:8000`.

## Nasazení na Render

1. Nahraj tuto složku do nového GitHub repozitáře.
2. V Renderu zvol **New → Blueprint**.
3. Připoj GitHub repozitář.
4. Render načte `render.yaml` a stránku nasadí.

## Veřejné API

- `/api/health`
- `/api/weather-markets`
- `/api/weather-markets?city=Tokyo`
- `/api/weather-markets?city=Tokyo&date=2026-07-15`
- `/api/weather-market?city=Tokyo&date=2026-07-15`

Data se krátce cachují, aby stránka zbytečně nezatěžovala Polymarket API.
