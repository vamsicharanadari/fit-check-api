import csv
import httpx
import os

from io import StringIO
from dotenv import load_dotenv
from bson import ObjectId
from fastapi import FastAPI, Body, UploadFile, File, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorClient

app = FastAPI()
load_dotenv()

# Read environment variables
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "fit-check-db")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CX = os.getenv("GOOGLE_CX")

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]
exerciseCollection = db["exercise"]
routineCollection = db["routine"]

@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


@app.on_event("startup")
async def startup_event():
    await exerciseCollection.create_index("title")


@app.get("/exercises/search")
async def search_google_images(title: str = Query(..., description="Exercise title to search or insert")):
    normalized_title = title.strip().lower()

    # Step 1: Search in DB
    query = {"title": {"$regex": f"^{normalized_title}$", "$options": "i"}}
    existing_doc = await exerciseCollection.find_one(query)

    if existing_doc:
        searched_gifs = existing_doc.get("searchedGifs", [])

        # Step 2: Return if gifs exist
        if searched_gifs:
            existing_doc["_id"] = str(existing_doc["_id"])
            return {"source": "db", "exercise": existing_doc}

        # Step 3: Fetch from Google, update DB
        image_urls = await fetch_google_gif_urls(normalized_title)
        if not image_urls:
            raise HTTPException(status_code=404, detail="No images found for that title")

        await exerciseCollection.update_one(query, {"$set": {
            "searchedGifs": image_urls,
            "gifUrl": image_urls[0]  # Optional main gif
        }})

        existing_doc["searchedGifs"] = image_urls
        existing_doc["gifUrl"] = image_urls[0]
        existing_doc["_id"] = str(existing_doc["_id"])
        return {"source": "google_update", "exercise": existing_doc}

    # Step 4: Not found in DB, fetch gifs and insert new document
    image_urls = await fetch_google_gif_urls(normalized_title)
    if not image_urls:
        raise HTTPException(status_code=404, detail="No images found for that title")

    new_doc = {
        "title": normalized_title,
        "description": "",
        "type": "",
        "body_part": "",
        "equipment": "",
        "level": "",
        "rating": 0.0,
        "rating_description": "",
        "gifUrl": image_urls[0],
        "searchedGifs": image_urls,
    }
    result = await exerciseCollection.insert_one(new_doc)
    new_doc["_id"] = str(result.inserted_id)
    return {"source": "google_insert", "exercise": new_doc}


# Helper to fetch gifs from Google
async def fetch_google_gif_urls(query_title: str):
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX,
        "q": f"{query_title} exercise gif",
        "searchType": "image",
        "num": 10,
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        if response.status_code != 200:
            return []
        data = response.json()
        return [item["link"] for item in data.get("items", [])]


@app.get("/exercises/{id}")
async def get_exercise_by_id(id: str):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    document = await exerciseCollection.find_one({"_id": obj_id})
    if not document:
        raise HTTPException(status_code=404, detail="Exercise not found")

    document["_id"] = str(document["_id"])
    return document


@app.get("/titles")
async def get_exercises_titles():
    cursor = exerciseCollection.find({}, {"title": 1, "_id": 0})  # Project only title field, exclude _id
    titles = []
    async for document in cursor:
        titles.append(document["title"])
    titles.sort()  # sort in-place
    return {"titles": titles}


@app.get("/exercises")
async def get_exercises():
    cursor = exerciseCollection.find()
    exercises = []
    async for document in cursor:
        document["_id"] = str(document["_id"])
        exercises.append(document)
    return {"exercises": exercises}


@app.post("/exercises/upload-csv")
async def upload_exercises_csv(file: UploadFile = File(...)):
    # Read CSV content
    content = await file.read()
    decoded = content.decode('utf-8')
    reader = csv.DictReader(StringIO(decoded))

    # Convert CSV rows to list of dicts
    exercises = []
    for row in reader:
        # Clean keys to match DB schema
        exercise = {
            "title": row.get("Title", "").strip().lower(),
            "description": row.get("Desc", "").strip(),
            "type": row.get("Type", "").strip(),
            "body_part": row.get("BodyPart", "").strip(),
            "equipment": row.get("Equipment", "").strip(),
            "level": row.get("Level", "").strip(),
            "rating": float(row.get("Rating", 0.0)) if row.get("Rating") else 0.0,
            "rating_description": row.get("RatingDesc", "").strip(),
            "gifUrl": "",
            "searchedGifs": []
        }
        exercises.append(exercise)

    # Insert into MongoDB
    if exercises:
        result = await exerciseCollection.insert_many(exercises)
        return {"inserted_count": len(result.inserted_ids)}
    else:
        return {"message": "No exercises found in CSV"}


@app.post("/exercise")
async def create_exercise(data: dict = Body(...)):
    result = await exerciseCollection.insert_one(data)
    return {"inserted_id": str(result.inserted_id)}


@app.post("/exercise/update-gif")
async def update_gif_url_by_id(data: dict = Body(...)):
    exercise_id = data.get("id")
    gif_url = data.get("gifUrl")

    if not exercise_id or not gif_url:
        raise HTTPException(status_code=400, detail="Both 'id' and 'gifUrl' are required.")

    try:
        obj_id = ObjectId(exercise_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid MongoDB ObjectId format.")

    result = await exerciseCollection.update_one(
        {"_id": obj_id},
        {"$set": {"gifUrl": gif_url}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"No exercise found with id '{exercise_id}'")

    return {"message": f"gifUrl updated for exercise with id '{exercise_id}'"}