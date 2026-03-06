import gspread
from google.oauth2.service_account import Credentials

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

try:
    print("Loading credentials...")
    creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    print("✅ Credentials loaded")

    print("Connecting to Google...")
    client = gspread.authorize(creds)
    print("✅ Client authorized")

    print("Opening sheet...")
    sheet = client.open_by_key("1nuXqst7Az550sjU5vfa1v9YjeJG-6H5xEy7GHwiR-kg")
    print("✅ Sheet opened")

    worksheet = sheet.sheet1
    print("Writing row...")
    worksheet.append_row(["TEST", "connection works!"])
    print("✅ Sheet connected successfully!")

except Exception as e:
    print(f"❌ Error: {e}")