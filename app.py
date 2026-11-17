import os
import re
from flask import Flask, render_template, request, redirect, url_for, session, flash
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from datetime import datetime
from bson import ObjectId
from bson.errors import InvalidId

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")  # needed for sessions

# --- Photo upload settings ---
UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB max per request

# --- MongoDB connection ---
# Local MongoDB by default; on the hosting site, MONGO_URI points to MongoDB Atlas
client = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017/"))
db = client["student_portal"]
users = db["users"]
users.create_index("email", unique=True)  # no two accounts with the same email
courses = db["courses"]
courses.create_index("code", unique=True)  # no two courses with the same code
registrations = db["registrations"]
# a student can register only once for the same course (enforced by the database)
registrations.create_index([("user_id", 1), ("course_id", 1)], unique=True)


# --- Seed default admin + lecturer (runs once, if missing) ---
def seed_users():
    if users.count_documents({"role": "admin"}) == 0:
        users.insert_one({
            "name": "Portal Admin", "email": "admin@portal.com",
            "password": generate_password_hash("admin123"),
            "role": "admin", "created_at": datetime.now()})
    if users.count_documents({"role": "lecturer"}) == 0:
        users.insert_one({
            "name": "Demo Lecturer", "email": "lecturer@portal.com",
            "password": generate_password_hash("lecturer123"),
            "role": "lecturer", "created_at": datetime.now()})


seed_users()


# --- Access control (decorator) ---
def login_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in first.", "error")
                return redirect(url_for("login"))
            if roles and session.get("role") not in roles:
                flash("You are not allowed to open that page.", "error")
                return redirect(url_for("home"))
            return fn(*args, **kwargs)
        return inner
    return wrapper


# --- Routes ---
@app.route("/")
def home():
    role = session.get("role")
    if role:
        return redirect(url_for(f"{role}_dashboard"))
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        reg_number = request.form["reg_number"].strip()
        password = request.form["password"]

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("signup.html")
        if users.find_one({"email": email}):
            flash("That email already has an account. Log in instead.", "error")
            return render_template("signup.html")

        users.insert_one({
            "name": name, "email": email, "reg_number": reg_number,
            "password": generate_password_hash(password),  # never store plain text
            "role": "student", "created_at": datetime.now()})
        flash("Account created. You can log in now.", "success")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = users.find_one({"email": email})

        if user and check_password_hash(user["password"], password):
            session["user_id"] = str(user["_id"])
            session["name"] = user["name"]
            session["role"] = user["role"]
            session["photo"] = user.get("photo")
            return redirect(url_for("home"))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Placeholder dashboards (we fill these in later steps) ---

def grade_letter(score):
    """Convert a score out of 100 into a letter."""
    if score is None:
        return ""
    for limit, letter in ((80, "A"), (70, "B"), (60, "C"), (50, "D")):
        if score >= limit:
            return letter
    return "F"


app.jinja_env.filters["letter"] = grade_letter

# ================= STUDENT : COURSES =================
def lecturer_name_map():
    return {u["_id"]: u["name"] for u in users.find({"role": "lecturer"})}


def seats_taken(course_id):
    # rejected registrations do not use a seat
    return registrations.count_documents({"course_id": course_id, "status": {"$ne": "rejected"}})


# READ : my registered courses
@app.route("/student")
@login_required("student")
def student_dashboard():
    uid = ObjectId(session["user_id"])
    names = lecturer_name_map()
    rows = []
    for r in registrations.find({"user_id": uid}).sort("date", -1):
        c = courses.find_one({"_id": r["course_id"]})
        if c:
            rows.append({"reg_id": r["_id"], "code": c["code"], "title": c["title"],
                         "credits": c["credits"], "status": r["status"], "date": r["date"],
                         "grade": r.get("grade"),
                         "lecturer": names.get(c.get("lecturer_id"), "Not assigned")})
    total = sum(x["credits"] for x in rows if x["status"] != "rejected")
    return render_template("student_dashboard.html", rows=rows, total_credits=total)


# READ : course catalog
@app.route("/student/courses")
@login_required("student")
def student_courses():
    uid = ObjectId(session["user_id"])
    mine = {r["course_id"]: r for r in registrations.find({"user_id": uid})}
    names = lecturer_name_map()
    catalog = []
    for c in courses.find().sort("code", 1):
        c["seats_left"] = max(c["capacity"] - seats_taken(c["_id"]), 0)
        c["lecturer_name"] = names.get(c.get("lecturer_id"), "Not assigned")
        reg = mine.get(c["_id"])
        c["my_status"] = reg["status"] if reg else None
        c["reg_id"] = reg["_id"] if reg else None
        catalog.append(c)
    return render_template("student_courses.html", catalog=catalog)


# CREATE : register for a course
@app.route("/student/register/<course_id>", methods=["POST"])
@login_required("student")
def register_course(course_id):
    uid = ObjectId(session["user_id"])
    try:
        course = courses.find_one({"_id": ObjectId(course_id)})
    except InvalidId:
        course = None
    if not course:
        flash("Course not found.", "error")
    elif registrations.find_one({"user_id": uid, "course_id": course["_id"]}):
        flash("You are already registered for this course.", "error")
    elif seats_taken(course["_id"]) >= course["capacity"]:
        flash(f"{course['code']} is full.", "error")
    else:
        try:
            registrations.insert_one({"user_id": uid, "course_id": course["_id"],
                                      "status": "pending", "grade": None,
                                      "date": datetime.now()})
            flash(f"Registered for {course['code']}. Waiting for admin approval.", "success")
        except DuplicateKeyError:
            flash("You are already registered for this course.", "error")
    return redirect(url_for("student_courses"))


# DELETE : drop a course (only your own registration)
@app.route("/student/drop/<reg_id>", methods=["POST"])
@login_required("student")
def drop_course(reg_id):
    try:
        result = registrations.delete_one({"_id": ObjectId(reg_id),
                                           "user_id": ObjectId(session["user_id"])})
        flash("Course dropped." if result.deleted_count else "Registration not found.",
              "success" if result.deleted_count else "error")
    except InvalidId:
        flash("Registration not found.", "error")
    if request.form.get("next") == "catalog":
        return redirect(url_for("student_courses"))
    return redirect(url_for("student_dashboard"))




@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    return render_template("admin_dashboard.html",
                           n_courses=courses.count_documents({}),
                           n_students=users.count_documents({"role": "student"}),
                           n_lecturers=users.count_documents({"role": "lecturer"}),
                           n_pending=registrations.count_documents({"status": "pending"}),
                           n_regs=registrations.count_documents({}))


# ================= ADMIN : COURSES (CRUD) =================
def get_lecturers():
    return list(users.find({"role": "lecturer"}))


def read_course_form(form):
    """Read and validate the course form. Returns (data, error)."""
    code = form["code"].strip().upper()
    title = form["title"].strip()
    try:
        credits = int(form["credits"])
        capacity = int(form["capacity"])
    except ValueError:
        return None, "Credits and capacity must be whole numbers."
    if not code or not title:
        return None, "Code and title are required."
    if credits < 1 or capacity < 1:
        return None, "Credits and capacity must be at least 1."
    lecturer_id = form.get("lecturer_id") or None
    return {"code": code, "title": title, "credits": credits,
            "capacity": capacity,
            "lecturer_id": ObjectId(lecturer_id) if lecturer_id else None}, None


# READ : list all courses
@app.route("/admin/courses")
@login_required("admin")
def admin_courses():
    lecturer_names = {u["_id"]: u["name"] for u in get_lecturers()}
    all_courses = list(courses.find().sort("code", 1))
    for c in all_courses:
        c["lecturer_name"] = lecturer_names.get(c.get("lecturer_id"), "Not assigned")
    return render_template("admin_courses.html", courses=all_courses)


# CREATE : add a course
@app.route("/admin/courses/add", methods=["GET", "POST"])
@login_required("admin")
def add_course():
    if request.method == "POST":
        data, error = read_course_form(request.form)
        if error:
            flash(error, "error")
        elif courses.find_one({"code": data["code"]}):
            flash(f"Course {data['code']} already exists.", "error")
        else:
            data["created_at"] = datetime.now()
            courses.insert_one(data)
            flash("Course added.", "success")
            return redirect(url_for("admin_courses"))
    return render_template("course_form.html", course=None,
                           lecturers=get_lecturers(), heading="Add a course")


# UPDATE : edit a course
@app.route("/admin/courses/<course_id>/edit", methods=["GET", "POST"])
@login_required("admin")
def edit_course(course_id):
    try:
        course = courses.find_one({"_id": ObjectId(course_id)})
    except InvalidId:
        course = None
    if not course:
        flash("Course not found.", "error")
        return redirect(url_for("admin_courses"))

    if request.method == "POST":
        data, error = read_course_form(request.form)
        clash = courses.find_one({"code": data["code"], "_id": {"$ne": course["_id"]}}) if data else None
        if error:
            flash(error, "error")
        elif clash:
            flash(f"Another course already uses code {data['code']}.", "error")
        else:
            courses.update_one({"_id": course["_id"]}, {"$set": data})
            flash("Course updated.", "success")
            return redirect(url_for("admin_courses"))
    return render_template("course_form.html", course=course,
                           lecturers=get_lecturers(), heading="Edit course")


# DELETE : remove a course (POST only, so a simple link cannot delete)
@app.route("/admin/courses/<course_id>/delete", methods=["POST"])
@login_required("admin")
def delete_course(course_id):
    try:
        result = courses.delete_one({"_id": ObjectId(course_id)})
        if result.deleted_count:
            registrations.delete_many({"course_id": ObjectId(course_id)})  # no orphan registrations
            flash("Course deleted.", "success")
        else:
            flash("Course not found.", "error")
    except InvalidId:
        flash("Course not found.", "error")
    return redirect(url_for("admin_courses"))




# ================= ADMIN : REGISTRATIONS (approve / reject) =================
STATUS_BY_ACTION = {"approve": "approved", "reject": "rejected"}


# READ : all registrations, with an optional status filter
@app.route("/admin/registrations")
@login_required("admin")
def admin_registrations():
    status = request.args.get("status", "")
    query = {"status": status} if status in ("pending", "approved", "rejected") else {}
    students = {u["_id"]: u for u in users.find({"role": "student"})}
    course_map = {c["_id"]: c for c in courses.find()}
    rows = []
    for r in registrations.find(query).sort("date", -1):
        s, c = students.get(r["user_id"]), course_map.get(r["course_id"])
        if s and c:
            rows.append({"reg_id": r["_id"], "student_id": s["_id"], "student": s["name"],
                         "reg_number": s.get("reg_number", ""), "photo": s.get("photo"),
                         "code": c["code"], "title": c["title"],
                         "date": r["date"], "status": r["status"]})
    counts = {st: registrations.count_documents({"status": st})
              for st in ("pending", "approved", "rejected")}
    counts["all"] = sum(counts.values())
    return render_template("admin_registrations.html", rows=rows, counts=counts, current=status)


# UPDATE : approve or reject a registration
@app.route("/admin/registrations/<reg_id>/<action>", methods=["POST"])
@login_required("admin")
def set_registration_status(reg_id, action):
    back = redirect(url_for("admin_registrations", status=request.form.get("filter", "")))
    if action not in STATUS_BY_ACTION:
        flash("Unknown action.", "error")
        return back
    try:
        reg = registrations.find_one({"_id": ObjectId(reg_id)})
    except InvalidId:
        reg = None
    if not reg:
        flash("Registration not found.", "error")
        return back

    new_status = STATUS_BY_ACTION[action]
    course = courses.find_one({"_id": reg["course_id"]})
    # a rejected registration freed its seat: check the course is not full before re-approving
    if new_status == "approved" and reg["status"] == "rejected" and course \
            and seats_taken(course["_id"]) >= course["capacity"]:
        flash(f"{course['code']} is full. Cannot approve.", "error")
        return back

    registrations.update_one({"_id": reg["_id"]},
                             {"$set": {"status": new_status, "decided_at": datetime.now()}})
    flash(f"Registration {new_status}.", "success")
    return back


# ================= ADMIN : STUDENTS (CRUD) =================
def find_student(student_id):
    try:
        return users.find_one({"_id": ObjectId(student_id), "role": "student"})
    except InvalidId:
        return None


def remove_photo_file(filename):
    if filename:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            os.remove(path)


# READ : list students, with search
@app.route("/admin/students")
@login_required("admin")
def admin_students():
    q = request.args.get("q", "").strip()
    query = {"role": "student"}
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"name": rx}, {"email": rx}, {"reg_number": rx}]
    students = list(users.find(query).sort("name", 1))
    for s in students:
        s["n_regs"] = registrations.count_documents({"user_id": s["_id"]})
    return render_template("admin_students.html", students=students, q=q)


# READ : one student with his registrations
@app.route("/admin/students/<student_id>")
@login_required("admin")
def admin_student_detail(student_id):
    s = find_student(student_id)
    if not s:
        flash("Student not found.", "error")
        return redirect(url_for("admin_students"))
    course_map = {c["_id"]: c for c in courses.find()}
    regs = []
    for r in registrations.find({"user_id": s["_id"]}).sort("date", -1):
        c = course_map.get(r["course_id"])
        if c:
            regs.append({"code": c["code"], "title": c["title"], "credits": c["credits"],
                         "status": r["status"], "date": r["date"]})
    return render_template("admin_student_detail.html", s=s, regs=regs)


# UPDATE : edit a student
@app.route("/admin/students/<student_id>/edit", methods=["GET", "POST"])
@login_required("admin")
def admin_edit_student(student_id):
    s = find_student(student_id)
    if not s:
        flash("Student not found.", "error")
        return redirect(url_for("admin_students"))

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        dob = request.form.get("date_of_birth", "").strip()
        error = None
        if not name or not email:
            error = "Name and email are required."
        elif users.find_one({"email": email, "_id": {"$ne": s["_id"]}}):
            error = "Another account already uses that email."
        elif dob:
            try:
                if datetime.strptime(dob, "%Y-%m-%d") > datetime.now():
                    error = "Date of birth cannot be in the future."
            except ValueError:
                error = "Invalid date of birth."
        if error:
            flash(error, "error")
        else:
            users.update_one({"_id": s["_id"]}, {"$set": {
                "name": name, "email": email,
                "reg_number": request.form.get("reg_number", "").strip(),
                "date_of_birth": dob,
                "gender": request.form.get("gender", ""),
                "phone": request.form.get("phone", "").strip(),
                "country": request.form.get("country", "").strip(),
                "updated_at": datetime.now()}})
            flash("Student updated.", "success")
            return redirect(url_for("admin_student_detail", student_id=student_id))
    return render_template("admin_student_form.html", s=s)


# DELETE : remove a student, his registrations and his photo
@app.route("/admin/students/<student_id>/delete", methods=["POST"])
@login_required("admin")
def admin_delete_student(student_id):
    s = find_student(student_id)
    if not s:
        flash("Student not found.", "error")
    else:
        registrations.delete_many({"user_id": s["_id"]})
        remove_photo_file(s.get("photo"))
        users.delete_one({"_id": s["_id"]})
        flash(f"Student {s['name']} deleted.", "success")
    return redirect(url_for("admin_students"))



# ================= ADMIN : LECTURERS (CRUD) =================
def find_lecturer(lecturer_id):
    try:
        return users.find_one({"_id": ObjectId(lecturer_id), "role": "lecturer"})
    except InvalidId:
        return None


# READ : list lecturers
@app.route("/admin/lecturers")
@login_required("admin")
def admin_lecturers():
    lecturers = list(users.find({"role": "lecturer"}).sort("name", 1))
    for l in lecturers:
        l["n_courses"] = courses.count_documents({"lecturer_id": l["_id"]})
    return render_template("admin_lecturers.html", lecturers=lecturers)


# CREATE : add a lecturer account
@app.route("/admin/lecturers/add", methods=["GET", "POST"])
@login_required("admin")
def admin_add_lecturer():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if not name or not email:
            flash("Name and email are required.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        elif users.find_one({"email": email}):
            flash("That email already has an account.", "error")
        else:
            users.insert_one({"name": name, "email": email,
                              "password": generate_password_hash(password),
                              "role": "lecturer", "created_at": datetime.now()})
            flash(f"Lecturer {name} created.", "success")
            return redirect(url_for("admin_lecturers"))
    return render_template("admin_lecturer_form.html", lecturer=None, heading="Add a lecturer")


# UPDATE : edit a lecturer (password optional)
@app.route("/admin/lecturers/<lecturer_id>/edit", methods=["GET", "POST"])
@login_required("admin")
def admin_edit_lecturer(lecturer_id):
    lecturer = find_lecturer(lecturer_id)
    if not lecturer:
        flash("Lecturer not found.", "error")
        return redirect(url_for("admin_lecturers"))
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form.get("password", "")
        if not name or not email:
            flash("Name and email are required.", "error")
        elif password and len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        elif users.find_one({"email": email, "_id": {"$ne": lecturer["_id"]}}):
            flash("Another account already uses that email.", "error")
        else:
            update = {"name": name, "email": email, "updated_at": datetime.now()}
            if password:
                update["password"] = generate_password_hash(password)
            users.update_one({"_id": lecturer["_id"]}, {"$set": update})
            flash("Lecturer updated.", "success")
            return redirect(url_for("admin_lecturers"))
    return render_template("admin_lecturer_form.html", lecturer=lecturer, heading="Edit lecturer")


# DELETE : remove a lecturer (his courses become "Not assigned")
@app.route("/admin/lecturers/<lecturer_id>/delete", methods=["POST"])
@login_required("admin")
def admin_delete_lecturer(lecturer_id):
    lecturer = find_lecturer(lecturer_id)
    if not lecturer:
        flash("Lecturer not found.", "error")
    else:
        courses.update_many({"lecturer_id": lecturer["_id"]}, {"$set": {"lecturer_id": None}})
        remove_photo_file(lecturer.get("photo"))
        users.delete_one({"_id": lecturer["_id"]})
        flash(f"Lecturer {lecturer['name']} deleted.", "success")
    return redirect(url_for("admin_lecturers"))


# ================= LECTURER : MY COURSES + GRADES =================
def find_own_course(course_id):
    """A lecturer can only open the courses assigned to him."""
    try:
        return courses.find_one({"_id": ObjectId(course_id),
                                 "lecturer_id": ObjectId(session["user_id"])})
    except InvalidId:
        return None


# READ : my courses
@app.route("/lecturer")
@login_required("lecturer")
def lecturer_dashboard():
    mine = list(courses.find({"lecturer_id": ObjectId(session["user_id"])}).sort("code", 1))
    for c in mine:
        c["approved"] = registrations.count_documents({"course_id": c["_id"], "status": "approved"})
        c["pending"] = registrations.count_documents({"course_id": c["_id"], "status": "pending"})
    return render_template("lecturer_dashboard.html", courses=mine)


# READ + UPDATE : approved students of one course, enter grades
@app.route("/lecturer/courses/<course_id>", methods=["GET", "POST"])
@login_required("lecturer")
def lecturer_course(course_id):
    course = find_own_course(course_id)
    if not course:
        flash("Course not found, or it is not assigned to you.", "error")
        return redirect(url_for("lecturer_dashboard"))

    regs = list(registrations.find({"course_id": course["_id"], "status": "approved"}))
    students = {u["_id"]: u for u in users.find({"_id": {"$in": [r["user_id"] for r in regs]}})}

    if request.method == "POST":
        updates, error = [], None
        for r in regs:
            raw = request.form.get(f"grade_{r['_id']}", "").strip()
            if raw == "":
                value = None
            else:
                try:
                    value = float(raw)
                except ValueError:
                    error = "Grades must be numbers."
                    break
                if not 0 <= value <= 100:
                    error = "Grades must be between 0 and 100."
                    break
                value = int(value) if value == int(value) else round(value, 1)
            updates.append((r["_id"], value))
        if error:
            flash(error, "error")
        else:
            for reg_id, value in updates:
                registrations.update_one({"_id": reg_id},
                                         {"$set": {"grade": value, "graded_at": datetime.now()}})
            flash("Grades saved.", "success")
        return redirect(url_for("lecturer_course", course_id=course_id))

    rows = []
    for r in regs:
        s = students.get(r["user_id"])
        if s:
            rows.append({"reg_id": r["_id"], "name": s["name"], "reg_number": s.get("reg_number", ""),
                         "photo": s.get("photo"), "grade": r.get("grade")})
    rows.sort(key=lambda x: x["name"].lower())
    return render_template("lecturer_course.html", course=course, rows=rows)


# ================= PROFILE (all roles) =================
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


@app.errorhandler(RequestEntityTooLarge)
def photo_too_large(e):
    flash("Photo too large. Maximum size is 2 MB.", "error")
    return redirect(url_for("profile"))


@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    user_id = ObjectId(session["user_id"])
    user = users.find_one({"_id": user_id})

    if request.method == "POST":
        name = request.form["name"].strip()
        dob = request.form.get("date_of_birth", "").strip()

        if not name:
            flash("Name cannot be empty.", "error")
            return redirect(url_for("profile"))
        if dob:
            try:
                if datetime.strptime(dob, "%Y-%m-%d") > datetime.now():
                    flash("Date of birth cannot be in the future.", "error")
                    return redirect(url_for("profile"))
            except ValueError:
                flash("Invalid date of birth.", "error")
                return redirect(url_for("profile"))

        update = {
            "name": name,
            "date_of_birth": dob,
            "gender": request.form.get("gender", ""),
            "phone": request.form.get("phone", "").strip(),
            "country": request.form.get("country", "").strip(),
            "updated_at": datetime.now(),
        }

        # --- photo: remove or replace ---
        old_photo = user.get("photo")
        if request.form.get("remove_photo") and old_photo:
            old_path = os.path.join(UPLOAD_FOLDER, old_photo)
            if os.path.exists(old_path):
                os.remove(old_path)
            update["photo"] = None

        file = request.files.get("photo")
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Photo must be PNG, JPG, JPEG or WEBP.", "error")
                return redirect(url_for("profile"))
            ext = file.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"{session['user_id']}.{ext}")
            if old_photo and old_photo != filename:
                old_path = os.path.join(UPLOAD_FOLDER, old_photo)
                if os.path.exists(old_path):
                    os.remove(old_path)
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            update["photo"] = filename  # MongoDB stores only the file NAME

        users.update_one({"_id": user_id}, {"$set": update})
        session["name"] = name
        session["photo"] = update.get("photo", old_photo)
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html", user=user)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
