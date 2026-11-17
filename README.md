# Student Course Portal

A web application where students register for courses, lecturers grade them, and an admin manages everything.
Backend: Python Flask + MongoDB (PyMongo). Frontend: HTML + CSS (Jinja templates).

## Features
| Role | What they can do |
|---|---|
| Student | Sign up, log in, edit profile (photo, date of birth...), browse courses, register / drop, see status and grades |
| Lecturer | See own courses, see approved students, enter grades (0-100) |
| Admin | CRUD on courses, students and lecturers; approve or reject registrations; assign lecturers to courses |

## Requirements
- Python 3.10+
- MongoDB running on `mongodb://localhost:27017` (MongoDB Compass can be used to view the data)

## How to run
```
cd student_portal
python -m pip install -r requirements.txt
python app.py
```
Open http://localhost:5000

## Hosted version
Deployed on Render, with the database on MongoDB Atlas. Settings are read from environment variables:
| Variable | Purpose |
|---|---|
| `MONGO_URI` | MongoDB connection string (defaults to local MongoDB) |
| `SECRET_KEY` | Secret used to sign login sessions |

Start command on the host: `gunicorn app:app`

## Default accounts (created automatically on first start)
| Role | Email | Password |
|---|---|---|
| Admin | admin@portal.com | admin123 |
| Lecturer | lecturer@portal.com | lecturer123 |

Students create their own account with "Create an account".

## Database: `student_portal`
| Collection | Main fields |
|---|---|
| users | name, email (unique), password (hashed), role, reg_number, date_of_birth, gender, phone, country, photo |
| courses | code (unique), title, credits, capacity, lecturer_id |
| registrations | user_id, course_id, status (pending/approved/rejected), grade, date |

`registrations` has a unique index on (user_id, course_id): a student cannot register twice for the same course.

## CRUD summary
| Entity | Create | Read | Update | Delete |
|---|---|---|---|---|
| Courses (admin) | Add course | Course list | Edit course | Delete course (also deletes its registrations) |
| Students (admin) | Sign up | Student list, detail, search | Edit student | Delete student (also deletes registrations and photo) |
| Lecturers (admin) | Add lecturer | Lecturer list | Edit lecturer | Delete lecturer (courses become unassigned) |
| Registrations | Student registers | Admin and student lists | Approve / reject, grades | Student drops a course |
| Profile (all roles) | - | View profile | Edit profile and photo | Remove photo |

## Security
- Passwords are hashed (werkzeug), never stored in plain text
- Sessions keep users logged in; every protected route checks the role (`@login_required`)
- Server-side validation of all forms, photo type (PNG/JPG/WEBP) and size (2 MB)
- Delete actions use POST only, with a confirmation

## Project structure
```
student_portal/
├── app.py              routes, MongoDB logic, security
├── requirements.txt
├── templates/          HTML pages (Jinja)
└── static/
    ├── style.css
    └── uploads/        profile photos
```
