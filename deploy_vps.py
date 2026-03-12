import paramiko
import sys
import time

def run_cmd(ssh, cmd):
    print(f"\\n>>> Running: {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd)
    
    # Send password to sudo if asked
    stdin.write('etex3lYamX\\n')
    stdin.flush()
    
    # Read outputs
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    exit_status = stdout.channel.recv_exit_status()
    
    if out: print("[STDOUT]", out)
    if err: print("[STDERR]", err)
    
    if exit_status != 0:
        print(f"!!! Command failed with status {exit_status}")
    return exit_status, out

def deploy():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        print("Connecting to VPS 217.61.208.21...")
        ssh.connect('217.61.208.21', port=22, username='console-BHBEgY', password='etex3lYamX', timeout=15)
        print("Successfully connected!")
        
        # Check OS
        run_cmd(ssh, "cat /etc/os-release")
        
        # Try to install git if missing
        run_cmd(ssh, "sudo -S dnf install -y git || sudo -S yum install -y git")
        
        # Check docker
        status, out = run_cmd(ssh, "docker --version")
        if status != 0:
            print("Installing Docker...")
            run_cmd(ssh, "sudo -S dnf config-manager --add-repo=https://download.docker.com/linux/centos/docker-ce.repo || sudo -S yum-config-manager --add-repo=https://download.docker.com/linux/centos/docker-ce.repo")
            run_cmd(ssh, "sudo -S dnf install -y docker-ce docker-ce-cli containerd.io || sudo -S yum install -y docker-ce docker-ce-cli containerd.io")
            run_cmd(ssh, "sudo -S systemctl enable docker && sudo -S systemctl start docker")
            run_cmd(ssh, "sudo -S usermod -aG docker $USER")
        
        # Check docker-compose
        status, out = run_cmd(ssh, "docker-compose --version")
        if status != 0:
            print("Installing Docker Compose...")
            run_cmd(ssh, "sudo -S curl -L 'https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-linux-x86_64' -o /usr/local/bin/docker-compose")
            run_cmd(ssh, "sudo -S chmod +x /usr/local/bin/docker-compose")
            run_cmd(ssh, "sudo -S ln -s /usr/local/bin/docker-compose /usr/bin/docker-compose || true")
        
        # Clone repository
        print("Cloning GitHub repository...")
        run_cmd(ssh, "rm -rf kalamobook")
        run_cmd(ssh, "git clone https://github.com/Dyland-Rada/kalamobook.git")
        
        # Build and Run Docker
        print("Starting Docker Container...")
        run_cmd(ssh, "cd kalamobook && sudo -S docker-compose up -d --build")
        
        print("Deployment sequence finished successfully!")
        
    except Exception as e:
        print(f"Failed to connect or deploy: {e}")
    finally:
        ssh.close()

if __name__ == '__main__':
    deploy()
