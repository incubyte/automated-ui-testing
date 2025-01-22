docker rm --force claude_computer_use_demo
docker build -t claude-computer-use-demo .
docker run -d -p 8080:8080 -p 8501:8501 -p 6080:6080  -v "C:/docker_mount":/home/docker_mount --env-file .env --name claude_computer_use_demo claude-computer-use-demo 