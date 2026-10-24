# SHRI JI ENTERPRISES — Smart Billing + Business AI v7

Full-stack local billing website built with **HTML + CSS + JavaScript + Python Flask + SQLite**.

## Main sections
- Dashboard
- New Bill
- Inventory
- Sales
- Alerts
- Locations
- Reports
- Settings

## Key features
- Billing with 20% discount
- Product catalog and inventory management
- Low-stock alerts
- Expiry / expired product alerts
- Sales history and bill details
- CSV export
- Stock adjustments and movement history
- Location management
- Daily/monthly/yearly/all-time reports
- JSON backup and restore
- Responsive mobile layout

## Run
```powershell
python -m venv env
.\env\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```
Open: http://127.0.0.1:5000

The database is created in `instance/smartbilling.db`.


## AI Assistant
- Added an AI Business Assistant page at `/ai`.
- The assistant can answer sales, inventory, category, low-stock, expiry, top-product and business-analysis questions using the existing SQLite data.
- The AI endpoint is read-only and does not modify products, bills or stock.
- Optional external AI: set `OPENAI_API_KEY` as an environment variable. Optionally set `OPENAI_MODEL`. No API key is stored in this project.

PowerShell example:
```powershell
$env:OPENAI_API_KEY="YOUR_KEY_HERE"
python app.py
```

Without an API key, the built-in SmartShop fallback still answers common data questions.
