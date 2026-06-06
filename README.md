
# Flask + Leaflet Starter

A minimal, professional-looking Flask app featuring a left-hand toolbar and a Leaflet map that covers most of the screen.

## Features
- Clean left sidebar with basic actions
- Full-viewport Leaflet map with OpenStreetMap tiles
- Responsive layout (collapses sidebar on small screens)
- Simple JS actions: Home, Locate Me (uses browser geolocation), Add Marker at center

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000

## Structure
```
flask_leaflet_app/
├── app.py
├── requirements.txt
├── README.md
├── static/
│   ├── css/
│   │   └── styles.css
│   └── js/
│       └── main.js
└── templates/
    └── index.html
```

## Notes
- Map tiles provided by OpenStreetMap via the standard public tile server. Please respect their usage policy for production apps.
- In production, run behind a WSGI server like Gunicorn and serve static assets efficiently.
