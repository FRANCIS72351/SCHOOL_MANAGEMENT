from app import app
from waitress import serve

if __name__ == "__main__":
    print("🚀 Server starting on http://127.0.0.1:3000")
    print("Visit http://localhost:3000 in your browser")
    serve(app, host='0.0.0.0', port=3000, threads=6)