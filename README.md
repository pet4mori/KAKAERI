# KAKARI

自分専用の工数管理・振り返りアプリです。

## ローカルで起動

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud で試す

1. GitHubにこのプロジェクトをアップロードする
2. https://share.streamlit.io/ にGitHubアカウントでログインする
3. `Create app` を押す
4. Repository にこのリポジトリを指定する
5. Branch は `main`
6. Main file path は `app.py`
7. `Deploy` を押す

## 注意

- `.env` はGitHubにアップロードしないでください。
- `kakari.db` はGitHubにアップロードしない設定にしています。
- Streamlit Community Cloudで試す場合、最初は空のDBから始まります。
- SQLiteはお試しには便利ですが、本格的にサイト運用するならクラウドDBへの移行がおすすめです。
