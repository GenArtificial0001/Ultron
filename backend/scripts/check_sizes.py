import os

base = r"C:\Proyectos\Jarvis con Claude\vision-web\backend\browser"

results = []
for name in os.listdir(base):
    full = os.path.join(base, name)
    if os.path.isdir(full):
        total = 0
        file_count = 0
        for root, dirs, files in os.walk(full):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                    file_count += 1
                except OSError:
                    pass
        results.append((name, total, file_count))

results.sort(key=lambda x: x[1], reverse=True)

for name, total, fc in results:
    mb = total / (1024 * 1024)
    print(f"{name} | {mb:.4f} MB | {total} bytes | {fc} archivos")
