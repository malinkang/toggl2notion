from setuptools import setup, find_packages

setup(
    name="toggl2notion",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "requests",
        "pendulum",
        "retrying",
        "notion-client",
        "python-dotenv",
        "emoji",
    ],
    entry_points={
        "console_scripts": [
            "toggl2notion = toggl2notion.toggl:main",
            "update_heatmap = toggl2notion.update_heatmap:main",
        ],
    },
    author="malinkang",
    author_email="linkang.ma@gmail.com",
    description="Sync Toggl time entries to Notion",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/malinkang/toggl2notion",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",
)
