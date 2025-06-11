import csv
import httpx
import os
import re
import json

from io import StringIO
from dotenv import load_dotenv
from bson import ObjectId
from fastapi import FastAPI, Body, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from typing import List, Optional

app = FastAPI()
load_dotenv()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or ["http://localhost:3000", "http://your-ip:port"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Path to your fallback file
FALLBACK_JSON_PATH = "fallback_routines.json"

# Global flag
db_connected = True
fallback_routines = []

# Read environment variables
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "fit-check-db")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CX = os.getenv("GOOGLE_CX")

# Initialize MongoDB client with TLS configuration
client = AsyncIOMotorClient(
    MONGO_URI,
    tls=True,
    tlsAllowInvalidCertificates=False,  # Never disable cert validation in production
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=20000,
    socketTimeoutMS=20000
)

db = client[DB_NAME]
exerciseCollection = db["exercise"]
routineCollection = db["routine"]


class RoutineModel(BaseModel):
    name: str = Field(..., example="Full Body Workout")
    description: Optional[str] = Field("", example="Covers all major muscle groups")
    exercise_ids: List[str] = Field(default_factory=list, example=["609e129e8c8b0c6f78f6901f"])

@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


@app.on_event("startup")
async def startup_event():
    global db_connected, fallback_routines
    try:
        await exerciseCollection.create_index("title")
    except Exception as e:
        print(f"DB connection/index creation failed: {e}")
        db_connected = False
        try:
            with open(FALLBACK_JSON_PATH, "r") as f:
                fallback_routines = json.load(f)
            print("Loaded fallback routines from JSON file.")
        except Exception as json_err:
            print(f"Failed to load fallback JSON file: {json_err}")


def clean_title(raw_title: str) -> str:
    cleaned = re.sub(r'[^A-Za-z\s]', '', raw_title).strip().lower()
    return ' '.join(word.capitalize() for word in cleaned.split())


@app.get("/exercises/title")
async def get_exercise_by_title(title: str = Query(..., description="Exact title to look up")):
    normalized_title = clean_title(title)
    query = {"title": {"$regex": f"^{normalized_title}$", "$options": "i"}}

    existing_doc = await exerciseCollection.find_one(query)

    if not existing_doc:
        raise HTTPException(status_code=404, detail="Exercise not found")

    # Convert _id to string before returning
    existing_doc["_id"] = str(existing_doc["_id"])
    return existing_doc


@app.get("/exercises/search")
async def search_google_images(title: str = Query(..., description="Exercise title to search or insert")):
    normalized_title = clean_title(title)

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
    seen_titles = set()
    exercises = []
    for row in reader:
        # Clean keys to match DB schema
        raw_title = row.get("Title", "").strip()

        # Clean title: remove special chars/numbers, lowercase
        cleaned = re.sub(r'[^A-Za-z\s]', '', raw_title).strip().lower()
        formatted_title = ' '.join(word.capitalize() for word in cleaned.split())

        if not formatted_title or formatted_title in seen_titles:
            continue  # Skip empty or duplicate titles

        seen_titles.add(formatted_title)

        exercise = {
            "title": formatted_title,
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


@app.get("/routines")
async def get_routines():
    if not db_connected:
        return {"routines": fallback_routines}

    routines = []
    cursor = routineCollection.find()
    async for document in cursor:
        document["_id"] = str(document["_id"])
        routines.append(document)
    return {"routines": routines}


@app.post("/routines")
async def create_routine(routine: RoutineModel):
    result = await routineCollection.insert_one(routine.dict())
    return {"inserted_id": str(result.inserted_id)}


@app.put("/routines/{id}")
async def update_routine(id: str, updated_data: RoutineModel):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    result = await routineCollection.update_one({"_id": obj_id}, {"$set": updated_data.dict()})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Routine not found")

    return {"message": "Routine updated successfully"}


@app.post("/routines/upload-csv")
async def upload_routines_csv(file: UploadFile = File(...)):
    content = await file.read()
    decoded = content.decode("utf-8")
    reader = csv.DictReader(StringIO(decoded))

    routine_map = {}

    for row in reader:
        routine_name = row.get("Routine", "").strip()
        muscle_group = row.get("MuscleGroup", "").strip()
        exercise_title = row.get("Exercise", "").strip()
        set_num = row.get("Set", "").strip()
        reps = row.get("Reps", "").strip()
        weight = row.get("Weight", "").strip()

        if not all([routine_name, muscle_group, exercise_title]):
            continue  # Skip incomplete rows

        # Nest structure: Routine -> MuscleGroup -> Exercise -> Sets
        routine = routine_map.setdefault(routine_name, {})
        group = routine.setdefault(muscle_group, {})
        exercise = group.setdefault(exercise_title, [])
        exercise.append([set_num, reps, weight])

    routines_to_insert = []
    for routine_name, groups in routine_map.items():
        group_list = []
        for group_name, exercises in groups.items():
            exercise_list = []
            for exercise_title, sets in exercises.items():
                exercise_list.append({
                    "title": exercise_title,
                    "table": sets
                })
            group_list.append({
                "title": group_name,
                "exercises": exercise_list
            })
        routines_to_insert.append({
            "name": routine_name,
            "description": "",  # You can customize this if needed
            "groups": group_list
        })

    if not routines_to_insert:
        return {"message": "No valid routines found in CSV"}

    result = await routineCollection.insert_many(routines_to_insert)
    return {
        "inserted_count": len(result.inserted_ids),
        "inserted_ids": [str(id) for id in result.inserted_ids]
    }