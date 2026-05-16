import {
	BufferAttribute,
	TriangleFanDrawMode,
	TriangleStripDrawMode,
	TrianglesDrawMode,
} from 'three';

function getIndexArray( index ) {
	if ( index !== null ) {
		return Array.from( index.array );
	}

	return null;
}

function makeTriangleIndexArray( geometry, drawMode ) {
	const index = getIndexArray( geometry.index );
	const vertexCount = index ? index.length : geometry.attributes.position.count;
	const triangles = [];

	for ( let i = 0; i < vertexCount - 2; i ++ ) {
		if ( drawMode === TriangleFanDrawMode ) {
			triangles.push(
				index ? index[ 0 ] : 0,
				index ? index[ i + 1 ] : i + 1,
				index ? index[ i + 2 ] : i + 2,
			);
		} else if ( drawMode === TriangleStripDrawMode ) {
			if ( i % 2 === 0 ) {
				triangles.push(
					index ? index[ i ] : i,
					index ? index[ i + 1 ] : i + 1,
					index ? index[ i + 2 ] : i + 2,
				);
			} else {
				triangles.push(
					index ? index[ i + 2 ] : i + 2,
					index ? index[ i + 1 ] : i + 1,
					index ? index[ i ] : i,
				);
			}
		}
	}

	return triangles;
}

function toTrianglesDrawMode( geometry, drawMode ) {
	if ( drawMode === TrianglesDrawMode ) {
		return geometry;
	}

	if ( drawMode !== TriangleFanDrawMode && drawMode !== TriangleStripDrawMode ) {
		return geometry;
	}

	if ( ! geometry.attributes.position ) {
		return geometry;
	}

	const newGeometry = geometry.clone();
	const indices = makeTriangleIndexArray( geometry, drawMode );
	newGeometry.setIndex( new BufferAttribute( new Uint32Array( indices ), 1 ) );
	newGeometry.clearGroups();

	return newGeometry;
}

export { toTrianglesDrawMode };
