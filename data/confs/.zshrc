# ============= XdebX =============

export ZSH="$HOME/.oh-my-zsh"
SH_THEME="jispwoso"
zstyle ':omz:update' mode auto  # audo / disabled

plugins=(
  z
  git
  sudo
  copypath
  history-substring-search
  extract
  systemd
  rsync
  aliases
  uv
  colored-man-pages
  command-not-found
  zsh-autosuggestions
  zsh-syntax-highlighting
)

source $ZSH/oh-my-zsh.sh

# export LANG=en_US.UTF-8

# ============= CONDA LAZY LOAD =============
_CONDA_ROOT="<PATH>/miniconda3"

conda() {
    unfunction conda
    local __conda_setup
    __conda_setup="$("$_CONDA_ROOT/bin/conda" 'shell.zsh' 'hook' 2> /dev/null)"
    if [ $? -eq 0 ]; then
        eval "$__conda_setup"
    else
        if [ -f "$_CONDA_ROOT/etc/profile.d/conda.sh" ]; then
            . "$_CONDA_ROOT/etc/profile.d/conda.sh"
        else
            export PATH="$_CONDA_ROOT/bin:$PATH"
        fi
    fi
    unset __conda_setup
    unset _CONDA_ROOT
    conda "$@"
}

# ============= USER ALIAS =============
alias ls="lsd"
alias ll="lsd -l"
alias la="lsd -a"
alias lla="lsd -la"
alias tree="lsd --tree"
alias claer="clear"
alias clare="clear"
alias clera="clear"
alias celar="clear"
alias sl="lsd"
alias zshrc="vim ~/.zshrc"
alias szsh="source ~/.zshrc"
alias bashrc="vim ~/.bashrc"
alias vimrc="vim ~/.vimrc"
alias 'cd..'="cd .."
alias 'start .'="xdg-open ."
alias ff="fastfetch"
alias docker="sudo docker"
alias t="tmux"
alias tls="tmux ls"
alias tn="tmux new -s "
alias ta="tmux attach -t "
alias td="tmux detach"
alias tk="tmux kill-session -t "
alias rsplit='tmux split-window -h -c "#{pane_current_path}"'
alias dsplit='tmux split-window -c "#{pane_current_path}"'
alias tp="tmuxp load"
alias bat="batcat"
alias clear_all="df -h && conda activate && conda clean --all -y && pip cache purge && sudo apt clean && uv cache clean && go clean -cache && df -h"

ff

# ============= USER FUNC =============
deb_install() {
    if [[ -z "$1" ]]; then
        echo "HELP: deb_install <package.deb>"
        return 1
    fi
    local DEB_FILE="$1"
    if [[ ! -f "$DEB_FILE" ]]; then
        echo "ERR: File not exist -> $DEB_FILE"
        return 1
    fi
    echo ">>> apt update"
    sudo apt update || return 1
    echo ">>> apt install  $DEB_FILE"
    if sudo apt install -y "./$DEB_FILE"; then
        echo "⭕ Install Success，del $DEB_FILE"
        rm -f "$DEB_FILE"
    else
        echo "❌ Install ERR，please try yourself: sudo apt install  $DEB_FILE"
        return 1
    fi
}

# ============= ROBOT =============

rros() {
  local choose=$1
  if [[ -z "$choose" ]]; then
    echo "ros: noetic(1) jazzy(2)?"
    read choose
  fi
  case $choose in
    1)
      [ -f /opt/ros/noetic/setup.zsh ] && source /opt/ros/noetic/setup.zsh
      [ -f /opt/ros/noetic_add/devel/setup.zsh ] && source /opt/ros/noetic_add/devel/setup.zsh
      alias R_tn="rosrun turtlesim turtlesim_node"
      alias R_tk="rosrun turtlesim turtle_teleop_key"
      alias R_source="source ./devel/setup.zsh"
      alias R_chmod='find src -type f -path "*/scripts/*.py" -exec chmod +x {} \;'
      alias R_catkin_build_auto="rm -rf ./build ./devel && catkin_make && source ./devel/setup.zsh"
      ;;
    2)
      [ -f /opt/ros/jazzy/setup.zsh ] && source /opt/ros/jazzy/setup.zsh
      ;;
    *) echo "ERR: $choose"; return 1 ;;
  esac

  # ===== NVIDIA PRIME Offload =====
  export __NV_PRIME_RENDER_OFFLOAD=1
  export __GLX_VENDOR_LIBRARY_NAME=nvidia
  export __VK_LAYER_NV_optimus=NVIDIA_only
}

# ======== DISP Mode =========
disp_type() {
    local mode="$1"
    if [[ "$mode" != "intel" && "$mode" != "nvidia" && "$mode" != "on-demand" ]]; then
        echo "HELP: disp_type {intel|nvidia|on-demand}"
        return 1
    fi
    echo "Change GPU mode: $mode"
    sudo prime-select "$mode"
    echo "Reboot..."
    sudo reboot
}
alias disos="disp_type on-demand"

# ======== Export =========
export PATH="$PATH:/usr/games"
export PATH="$HOME/.bin:$PATH"
export PATH="$HOME/.local/bin:$PATH"

# ignore nvm default zshrc insert and use this
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"

# Goland
export GOROOT="/opt/go"
export GOPATH="/opt/gopath"
export PATH="$PATH:$GOROOT/bin:$GOPATH/bin"
