import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { GLTFLoader } from './vendor/GLTFLoader.js';

const canvas = document.getElementById('viewerCanvas');
const statusEl = document.getElementById('status');
const emptyStateEl = document.getElementById('emptyState');
const characterCountEl = document.getElementById('characterCount');
const characterListEl = document.getElementById('characterList');
const searchEl = document.getElementById('characterSearch');
const animationSelectEl = document.getElementById('animationSelect');
const animationStatusEl = document.getElementById('animationStatus');
const wireframeEl = document.getElementById('toggleWireframe');
const skeletonEl = document.getElementById('toggleSkeleton');
const loopEl = document.getElementById('loopToggle');

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1b2025);

const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 2000);
camera.position.set(0, 1.6, 4.5);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.target.set(0, 1, 0);

scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.3));
const ambient = new THREE.AmbientLight(0xffffff, 0.35);
scene.add(ambient);
const sun = new THREE.DirectionalLight(0xffffff, 1.4);
sun.position.set(4, 7, 6);
scene.add(sun);

const grid = new THREE.GridHelper(8, 16, 0x5d6973, 0x313942);
grid.position.y = -0.01;
scene.add(grid);

const loader = new GLTFLoader();
const clock = new THREE.Clock();

let characterEntries = [];
let currentRoot = null;
let currentMixer = null;
let currentClips = [];
let currentAction = null;
let currentSkeletonHelper = null;
let activeSlug = null;

function setStatus(message) {
  statusEl.textContent = message;
}

function setAnimationStatus(message) {
  animationStatusEl.textContent = message;
}

function updateRendererSize() {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) {
    return;
  }
  renderer.setSize(rect.width, rect.height, false);
  camera.aspect = rect.width / rect.height;
  camera.updateProjectionMatrix();
}

function disposeCurrentModel() {
  if (currentSkeletonHelper) {
    scene.remove(currentSkeletonHelper);
    currentSkeletonHelper = null;
  }
  if (currentRoot) {
    scene.remove(currentRoot);
    currentRoot.traverse((object) => {
      if (object.isMesh) {
        object.geometry?.dispose?.();
        if (Array.isArray(object.material)) {
          object.material.forEach((material) => material.dispose?.());
        } else {
          object.material?.dispose?.();
        }
      }
    });
  }
  currentRoot = null;
  currentMixer = null;
  currentClips = [];
  currentAction = null;
  animationSelectEl.innerHTML = '';
}

function applyWireframe(enabled) {
  if (!currentRoot) {
    return;
  }
  currentRoot.traverse((object) => {
    if (!object.isMesh) {
      return;
    }
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    materials.filter(Boolean).forEach((material) => {
      material.wireframe = enabled;
      material.needsUpdate = true;
    });
  });
}

function updateSkeletonVisibility() {
  if (currentSkeletonHelper) {
    currentSkeletonHelper.visible = skeletonEl.checked;
  }
}

function fitCameraToObject(root) {
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) {
    return;
  }
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 0.5);
  const distance = radius / Math.tan(THREE.MathUtils.degToRad(camera.fov * 0.5)) * 1.3;
  camera.position.copy(center).add(new THREE.Vector3(distance * 0.65, distance * 0.45, distance));
  camera.near = Math.max(radius / 100, 0.01);
  camera.far = Math.max(radius * 20, 100);
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
}

function populateAnimations(animations) {
  animationSelectEl.innerHTML = '';
  if (!animations.length) {
    const option = document.createElement('option');
    option.textContent = 'No animations';
    option.value = '';
    animationSelectEl.append(option);
    animationSelectEl.disabled = true;
    setAnimationStatus('This GLB does not contain animation clips.');
    return;
  }

  animationSelectEl.disabled = false;
  animations.forEach((clip, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = clip.name || `Animation ${index + 1}`;
    animationSelectEl.append(option);
  });
  setAnimationStatus(`${animations.length} animation clip(s) available.`);
}

function playSelectedAnimation() {
  if (!currentMixer || !currentClips.length) {
    return;
  }
  const clip = currentClips[Number(animationSelectEl.value) || 0];
  if (!clip) {
    return;
  }
  if (currentAction) {
    currentAction.stop();
  }
  currentAction = currentMixer.clipAction(clip);
  currentAction.reset();
  currentAction.setLoop(loopEl.checked ? THREE.LoopRepeat : THREE.LoopOnce, loopEl.checked ? Infinity : 1);
  currentAction.clampWhenFinished = !loopEl.checked;
  currentAction.paused = false;
  currentAction.play();
  setAnimationStatus(`Playing: ${clip.name || 'unnamed clip'}`);
}

function pauseAnimation() {
  if (currentAction) {
    currentAction.paused = !currentAction.paused;
    setAnimationStatus(currentAction.paused ? 'Animation paused.' : 'Animation resumed.');
  }
}

function stopAnimation() {
  if (currentAction) {
    currentAction.stop();
    currentAction = null;
    setAnimationStatus('Animation stopped.');
  }
}

async function loadCharacter(entry) {
  activeSlug = entry.slug;
  setStatus(`Loading ${entry.name}…`);
  emptyStateEl.hidden = true;
  disposeCurrentModel();

  try {
    const gltf = await loader.loadAsync(entry.glb);
    currentRoot = gltf.scene;
    currentRoot.name = entry.name;
    scene.add(currentRoot);

    currentMixer = gltf.animations.length ? new THREE.AnimationMixer(currentRoot) : null;
    currentClips = gltf.animations;
    populateAnimations(currentClips);

    const skeletonTarget = currentRoot.getObjectByProperty('type', 'SkinnedMesh');
    if (skeletonTarget) {
      currentSkeletonHelper = new THREE.SkeletonHelper(currentRoot);
      currentSkeletonHelper.visible = skeletonEl.checked;
      scene.add(currentSkeletonHelper);
    }

    applyWireframe(wireframeEl.checked);
    updateSkeletonVisibility();
    fitCameraToObject(currentRoot);
    setStatus(`Loaded ${entry.name}`);
    if (currentClips.length) {
      playSelectedAnimation();
    }
  } catch (error) {
    console.error(error);
    emptyStateEl.hidden = false;
    emptyStateEl.textContent = 'Failed to load character GLB.';
    setStatus(`Failed to load ${entry.name}`);
    setAnimationStatus(String(error));
  }

  renderCharacterList();
}

function renderCharacterList() {
  const query = searchEl.value.trim().toLowerCase();
  const filtered = characterEntries.filter((entry) => {
    return !query || entry.name.toLowerCase().includes(query) || entry.slug.toLowerCase().includes(query);
  });

  characterListEl.innerHTML = '';
  characterCountEl.textContent = `${filtered.length} character(s)`;

  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'character-item__meta';
    empty.textContent = 'No matching extracted characters.';
    characterListEl.append(empty);
    return;
  }

  filtered.forEach((entry) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'character-item';
    if (entry.slug === activeSlug) {
      button.classList.add('is-active');
    }
    button.innerHTML = `
      <div>${entry.name}</div>
      <div class="character-item__meta">${entry.triangleCount || 0} tris · ${entry.textureCount || 0} textures</div>
    `;
    button.addEventListener('click', () => {
      loadCharacter(entry);
    });
    characterListEl.append(button);
  });
}

async function loadIndex() {
  setStatus('Loading character index…');
  try {
    const response = await fetch('/api/characters');
    const payload = await response.json();
    characterEntries = payload.characters || [];
    characterEntries.sort((a, b) => a.name.localeCompare(b.name));
    renderCharacterList();
    if (!characterEntries.length) {
      emptyStateEl.hidden = false;
      emptyStateEl.textContent = 'No extracted characters yet. Run the extractor first.';
      setStatus('No extracted characters found');
    } else {
      setStatus('Select a character to preview');
    }
  } catch (error) {
    console.error(error);
    emptyStateEl.hidden = false;
    emptyStateEl.textContent = 'Failed to load character index.';
    setStatus('Failed to load index');
  }
}

function animate() {
  requestAnimationFrame(animate);
  const delta = clock.getDelta();
  controls.update();
  currentMixer?.update(delta);
  renderer.render(scene, camera);
}

window.addEventListener('resize', updateRendererSize);
searchEl.addEventListener('input', renderCharacterList);
wireframeEl.addEventListener('change', () => applyWireframe(wireframeEl.checked));
skeletonEl.addEventListener('change', updateSkeletonVisibility);
document.getElementById('resetCamera').addEventListener('click', () => {
  if (currentRoot) {
    fitCameraToObject(currentRoot);
  }
});
document.getElementById('playAnimation').addEventListener('click', playSelectedAnimation);
document.getElementById('pauseAnimation').addEventListener('click', pauseAnimation);
document.getElementById('stopAnimation').addEventListener('click', stopAnimation);
animationSelectEl.addEventListener('change', () => {
  if (currentClips.length) {
    playSelectedAnimation();
  }
});
loopEl.addEventListener('change', () => {
  if (currentAction) {
    playSelectedAnimation();
  }
});

updateRendererSize();
loadIndex();
animate();
