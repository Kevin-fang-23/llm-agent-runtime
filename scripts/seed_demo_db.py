"""初始化演示业务库 data/demo.sqlite（校园主题，供 db_query 工具查询）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402


def seed(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        DROP TABLE IF EXISTS students;
        DROP TABLE IF EXISTS courses;
        DROP TABLE IF EXISTS scores;
        CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, major TEXT, grade INT);
        CREATE TABLE courses (id INTEGER PRIMARY KEY, name TEXT, credit REAL);
        CREATE TABLE scores (
            student_id INT, course_id INT, score REAL,
            FOREIGN KEY(student_id) REFERENCES students(id),
            FOREIGN KEY(course_id) REFERENCES courses(id)
        );
        INSERT INTO students (name, major, grade) VALUES
            ('张三', '计算机科学', 2024), ('李四', '软件工程', 2023),
            ('王五', '数据科学', 2024), ('赵六', '计算机科学', 2022),
            ('钱七', '软件工程', 2024);
        INSERT INTO courses (name, credit) VALUES
            ('机器学习', 3.0), ('数据库系统', 2.5), ('编译原理', 3.5),
            ('操作系统', 3.0), ('深度学习', 2.0);
        INSERT INTO scores (student_id, course_id, score) VALUES
            (1,1,92),(1,2,88),(1,5,95),(2,1,85),(2,3,78),(3,1,90),(3,2,93),
            (4,4,82),(5,1,87),(5,4,80);
        """
    )
    conn.commit()
    conn.close()
    print(f"演示库已初始化: {db_path}")


if __name__ == "__main__":
    seed(get_settings().tool_db_path)
