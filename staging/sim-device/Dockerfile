# Simulated samcloud device — real sshd, configurable identity
# Build: docker build -t samcloud-sim-device .
# Run:   docker run -d -p 2201:22 -e DEVICE_ID=sim-media -e DEVICE_TYPE=server samcloud-sim-device
FROM alpine:3.20

RUN apk add --no-cache openssh-server openssh-client bash curl python3 && \
    ssh-keygen -A && \
    adduser -D -s /bin/bash samcloud && \
    echo "samcloud:samcloud" | chpasswd && \
    mkdir -p /home/samcloud/.ssh && \
    chown -R samcloud:samcloud /home/samcloud/.ssh && \
    chmod 700 /home/samcloud/.ssh

# Health endpoint (simple HTTP server)
COPY health-server.py /opt/health-server.py

# Entrypoint configures identity and starts sshd + health
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22 8080

ENTRYPOINT ["/entrypoint.sh"]
