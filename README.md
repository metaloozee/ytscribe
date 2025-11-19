# YTScribe

YTScribe is a command-line interface (CLI) tool designed to easily download YouTube video transcripts and video files. It supports processing individual videos, multiple URLs, and entire playlists.

## Features

- Download transcripts from single YouTube videos
- Download transcripts from multiple YouTube videos 
- Download transcripts from entire YouTube playlists
- Optionally download video files alongside transcripts

## Installation

To install YTScribe locally as a CLI tool, follow these steps:

1.  **Prerequisites**: Ensure you have Python installed. It is recommended to use [uv](https://github.com/astral-sh/uv) for building the project.

2.  **Build and Install**:

    Run the following commands in the project root:

    ```bash
    uv build
    pip install .
    ```

    This will build the package and install it into your current environment.

3.  **Usage**:

    Once installed, you can run the tool using `ytscribe` or the alias `yts`:

    ```bash
    ytscribe --help
    # or
    yts --help
    ```
