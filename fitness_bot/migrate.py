import sqlite3

conn = sqlite3.connect('bot.db')
c = conn.cursor()
cols = [r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()]
new_cols = {
    'disliked_foods': "'[]'",
    'dietary_preferences': "'[]'",
    'cooking_level': "'medium'",
    'food_notes': "''",
    'role': "'user'",
}
for col, default in new_cols.items():
    if col not in cols:
        c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT {default}")
        print(f'Added: {col}')
    else:
        print(f'Exists: {col}')
conn.commit()
conn.close()
print('Migration done')
